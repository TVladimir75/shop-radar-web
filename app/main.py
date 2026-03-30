from __future__ import annotations

import asyncio
import base64
import csv
import os
import io
import logging
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import File, FastAPI, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.platform_links import platform_search_urls
from app.query_translate import prepare_query_for_platforms
from app.search_cache import get_cached, set_cached
from app.search_pipeline import (
    attach_platform_links,
    filter_scored,
    score_rows,
)
from app.scrapers.alibaba_scraper import fetch_suppliers as fetch_alibaba
from app.scrapers.cn1688 import fetch_suppliers as fetch_1688
from app.scrapers.mic import fetch_suppliers as fetch_mic
from app.scrapers.pinduoduo_scraper import fetch_suppliers as fetch_pinduoduo
from app.scrapers.taobao_scraper import fetch_suppliers as fetch_taobao
from app.site_meta import footer_context, landing_context, tool_href
from app.suggestions import suggest as suggest_products

BASE = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
logger = logging.getLogger("uvicorn.error")


def _page_ctx(request: Request, **kwargs):
    base = str(request.base_url).rstrip("/")
    return {**footer_context(), "page_base_url": base, **kwargs}


SITES = [
    {"id": "all", "label": "Все: MIC + Alibaba + 1688 + Taobao", "active": True},
    {"id": "mic", "label": "Made-in-China.com", "active": True},
    {"id": "alibaba", "label": "Alibaba.com (showroom)", "active": True},
    {"id": "1688", "label": "1688.com", "active": True},
    {"id": "taobao", "label": "Taobao.com", "active": True},
    {"id": "pinduoduo", "label": "Pinduoduo.com", "active": True},
]

_SITE_IDS = {s["id"] for s in SITES}


def _normalize_site_id(site_id: str) -> str:
    s = (site_id or "all").strip()
    return s if s in _SITE_IDS else "all"


async def _wait_first(
    fn,
    product: str,
    limit: int,
    timeout: float | None,
    *,
    retries: int = 1,
):
    last: asyncio.TimeoutError | None = None
    for attempt in range(retries + 1):
        try:
            if timeout is not None:
                return await asyncio.wait_for(
                    fn(product, limit=limit), timeout=timeout
                )
            return await fn(product, limit=limit)
        except asyncio.TimeoutError as e:
            last = e
            if attempt >= retries:
                raise
    raise last  # pragma: no cover


async def _fetch_raw_rows(product: str, site_id: str) -> tuple[list[dict], str | None]:
    notes: list[str] = []
    merged: list[dict] = []

    if site_id == "mic":
        merged = await fetch_mic(product, limit=35)
    elif site_id == "alibaba":
        merged = await fetch_alibaba(product, limit=35)
    elif site_id == "1688":
        try:
            merged = await _wait_first(fetch_1688, product, 30, 20.0, retries=1)
        except asyncio.TimeoutError:
            raise ValueError(
                "1688.com: нет ответа за отведённое время (часто недоступен вне Китая)."
            ) from None
    elif site_id == "taobao":
        try:
            merged = await _wait_first(fetch_taobao, product, 30, 18.0, retries=1)
        except asyncio.TimeoutError:
            raise ValueError(
                "Taobao: нет ответа за отведённое время (часто недоступен вне Китая)."
            ) from None
    elif site_id == "pinduoduo":
        merged = await fetch_pinduoduo(product, limit=30)
    elif site_id == "all":
        for fn, label, lim, tmo in (
            (fetch_mic, "Made-in-China", 18, None),
            (fetch_alibaba, "Alibaba", 18, None),
            (fetch_1688, "1688", 8, 16.0),
            (fetch_taobao, "Taobao", 8, 14.0),
        ):
            try:
                if label in ("1688", "Taobao") and tmo is not None:
                    part = await _wait_first(fn, product, lim, tmo, retries=1)
                elif tmo is not None:
                    part = await asyncio.wait_for(
                        fn(product, limit=lim), timeout=tmo
                    )
                else:
                    part = await fn(product, limit=lim)
                merged.extend(part)
            except asyncio.TimeoutError:
                notes.append(
                    f"{label}: нет ответа за {int(tmo or 0)} с (часто недоступен вне Китая — остальные площадки уже в таблице)"
                )
            except Exception as e:
                notes.append(f"{label}: {e!s}")
    else:
        raise ValueError("Неизвестная площадка")

    return merged, (" · ".join(notes) if notes else None)


async def _load_rows(
    product_key: str, site_id: str
) -> tuple[list[dict], str | None, bool]:
    hit = await get_cached(site_id, product_key)
    if hit is not None:
        rows, notes = hit
        return rows, notes, True
    rows, notes = await _fetch_raw_rows(product_key, site_id)
    await set_cached(site_id, product_key, rows, notes)
    return rows, notes, False


def _parse_only_flag(v: int | str) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "on")


def _export_query(
    product: str,
    site_id: str,
    filter_site: str,
    name_contains: str,
    only_price: bool,
    only_mfg: bool,
) -> str:
    return urlencode(
        {
            "product": product or "",
            "site_id": site_id,
            "filter_site": filter_site or "",
            "name_contains": name_contains or "",
            "only_price": 1 if only_price else 0,
            "only_mfg": 1 if only_mfg else 0,
        },
        doseq=True,
    )


async def _build_search_block_context(
    request: Request,
    *,
    product: str,
    site_id: str,
    filter_site: str = "",
    name_contains: str = "",
    only_price: bool = False,
    only_manufacturer: bool = False,
) -> dict:
    site_id = _normalize_site_id(site_id)
    fs = (filter_site or "").strip().lower()
    if fs not in {"", "mic", "alibaba", "1688", "taobao", "pinduoduo"}:
        fs = ""
    name_contains = (name_contains or "").strip()

    error: str | None = None
    merge_note: str | None = None
    cache_note: str | None = None
    row_count_unfiltered = 0
    query_used = ""

    product_stripped = (product or "").strip()
    if not product_stripped:
        return {
            "rows": None,
            "row_count_unfiltered": 0,
            "query_used": "",
            "platform_urls": {},
            "error": None,
            "merge_note": None,
            "cache_note": None,
            "filter_site": fs,
            "name_contains": name_contains,
            "only_price": only_price,
            "only_mfg": only_manufacturer,
            "site_id": site_id,
            "query": "",
            "export_qs": _export_query(
                "", site_id, fs, name_contains, only_price, only_manufacturer
            ),
        }

    rows_out: list[dict] | None = None
    try:
        query_used = await prepare_query_for_platforms(product_stripped)
        raw, merge_note, from_cache = await _load_rows(query_used, site_id)
        if from_cache:
            cache_note = "Показан кэш сырой выдачи (~15 мин), без повторного запроса к площадкам."

        scored = score_rows(raw)
        row_count_unfiltered = len(scored)
        urls = platform_search_urls(query_used)
        attach_platform_links(scored, urls)
        filtered = filter_scored(
            scored,
            filter_site_key=fs,
            name_contains=name_contains,
            only_with_price=only_price,
            only_manufacturer=only_manufacturer,
        )
        rows_out = filtered

        if not raw:
            error = (
                "Ничего не найдено. Попробуйте другие слова; на MIC/Alibaba на английском обычно лучше."
            )
        elif not filtered and raw:
            error = "Ни одна строка не прошла фильтры. Ослабьте условия или снимите галочки."

    except ValueError as e:
        error = str(e)
    except Exception as e:
        error = f"Не удалось загрузить данные: {e!s}"

    return {
        "rows": rows_out,
        "row_count_unfiltered": row_count_unfiltered,
        "query_used": query_used,
        "platform_urls": platform_search_urls(query_used) if query_used else {},
        "error": error,
        "merge_note": merge_note,
        "cache_note": cache_note,
        "filter_site": fs,
        "name_contains": name_contains,
        "only_price": only_price,
        "only_mfg": only_manufacturer,
        "site_id": site_id,
        "query": product_stripped,
        "export_qs": _export_query(
            product_stripped,
            site_id,
            fs,
            name_contains,
            only_price,
            only_manufacturer,
        ),
    }


def _render_search_block_html(request: Request, ctx: dict) -> str:
    return templates.get_template("partials/search_block.html").render(
        request=request, **ctx
    )


async def _render_search_page(
    request: Request,
    product: str,
    site_id: str,
    *,
    filter_site: str = "",
    name_contains: str = "",
    only_price: bool = False,
    only_mfg: bool = False,
) -> HTMLResponse:
    block = await _build_search_block_context(
        request,
        product=product,
        site_id=site_id,
        filter_site=filter_site,
        name_contains=name_contains,
        only_price=only_price,
        only_manufacturer=only_mfg,
    )
    return templates.TemplateResponse(
        request,
        "index.html",
        _page_ctx(
            request,
            sites=SITES,
            search_block_html=_render_search_block_html(request, block),
            **{k: block[k] for k in (
                "rows", "row_count_unfiltered", "query_used", "platform_urls",
                "error", "merge_note", "cache_note",
                "filter_site", "name_contains", "only_price", "only_mfg",
                "site_id", "query", "export_qs",
            )},
        ),
    )


app = FastAPI(title="Подбор поставщиков")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request) -> HTMLResponse:
    tool_rel = str(request.url_for("tool_home"))
    ctx = {**landing_context(), "tool_url": tool_href(tool_rel)}
    return templates.TemplateResponse(request, "landing.html", ctx)


@app.get("/tool", response_class=HTMLResponse, name="tool_home")
async def tool_home(request: Request) -> HTMLResponse:
    empty = await _build_search_block_context(
        request, product="", site_id="all"
    )
    return templates.TemplateResponse(
        request,
        "index.html",
        _page_ctx(
            request,
            sites=SITES,
            search_block_html=_render_search_block_html(request, empty),
            **{k: empty[k] for k in (
                "rows", "row_count_unfiltered", "query_used", "platform_urls",
                "error", "merge_note", "cache_note",
                "filter_site", "name_contains", "only_price", "only_mfg",
                "site_id", "query", "export_qs",
            )},
        ),
    )


@app.get("/api/suggest")
async def api_suggest(q: str = Query("", max_length=120)) -> JSONResponse:
    items = suggest_products(q, limit=12)
    return JSONResponse({"items": items})


@app.get("/api/translate_query")
async def api_translate_query(q: str = Query("", max_length=300)) -> JSONResponse:
    raw = (q or "").strip()
    if not raw:
        return JSONResponse({"en": "", "changed": False})
    en = await prepare_query_for_platforms(raw)
    changed = en != raw
    return JSONResponse({"en": en, "changed": changed})


@app.post("/api/vision-to-query")
async def api_vision_to_query(image: UploadFile = File(...)) -> JSONResponse:
    """
    Берёт загруженное фото, вызывает Google Vision API и возвращает текстовый запрос
    (label/text) для передачи в существующий поиск.
    """
    key = os.environ.get("GOOGLE_VISION_KEY")
    if not key:
        return JSONResponse(
            {"error": "Поиск по фото сейчас недоступен."},
            status_code=400,
        )

    # Ограничение, чтобы не убить сервер огромными файлами.
    raw = await image.read()
    if len(raw) > 4 * 1024 * 1024:
        return JSONResponse({"error": "Файл слишком большой (лимит 4MB)."}, status_code=400)

    b64 = base64.b64encode(raw).decode("ascii")
    url = "https://vision.googleapis.com/v1/images:annotate"

    payload = {
        "requests": [
            {
                "image": {"content": b64},
                "features": [
                    {"type": "LABEL_DETECTION", "maxResults": 5},
                    {"type": "TEXT_DETECTION", "maxResults": 1},
                ],
                # Подсказка для модели (если ключ возвращает русский — всё равно ок).
                "imageContext": {"languageHints": ["en", "ru"]},
            }
        ]
    }

    logger.warning("vision_request_start size_bytes=%s", len(raw))
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, params={"key": key}, json=payload)
            r.raise_for_status()
            logger.warning(
                "vision_request_ok status=%s response_bytes=%s",
                r.status_code,
                len(r.text or ""),
            )
    except httpx.HTTPStatusError as e:
        body_preview = (e.response.text or "")[:400].replace("\n", " ")
        logger.error(
            "vision_request_http_status_error status=%s body_preview=%s",
            e.response.status_code,
            body_preview,
        )
        return JSONResponse(
            {"error": "Не удалось распознать фото. Попробуйте другое изображение или позже."},
            status_code=502,
        )
    except httpx.HTTPError as e:
        logger.error("vision_request_http_error type=%s detail=%s", type(e).__name__, str(e))
        return JSONResponse(
            {"error": "Не удалось распознать фото. Попробуйте позже."},
            status_code=502,
        )

    data = r.json()
    resp = ((data or {}).get("responses") or [{}])[0]
    labels = resp.get("labelAnnotations") or []
    text_ann = resp.get("textAnnotations") or []

    query = ""
    if isinstance(labels, list) and labels:
        # description обычно наиболее “человеческая”
        query = (labels[0].get("description") or "").strip()
    if not query and isinstance(text_ann, list) and text_ann:
        # 0-й элемент textAnnotations — полный распознанный текст
        query = (text_ann[0].get("description") or "").strip()

    query = query[:120] if query else ""
    if not query:
        return JSONResponse(
            {
                "error": "Не удалось извлечь осмысленный запрос из фото. Попробуйте другое изображение/ракурс.",
                "debug": {"labels_count": len(labels) if isinstance(labels, list) else 0},
            },
            status_code=400,
        )

    return JSONResponse({"query": query})


@app.get("/api/search-result", name="api_search_result")
async def api_search_result(
    request: Request,
    product: str = Query("", max_length=300),
    site_id: str = Query("all"),
    filter_site: str = Query("", max_length=32),
    name_contains: str = Query("", max_length=200),
    only_price: int = Query(0),
    only_mfg: int = Query(0),
) -> JSONResponse:
    ctx = await _build_search_block_context(
        request,
        product=product,
        site_id=site_id,
        filter_site=filter_site,
        name_contains=name_contains,
        only_price=_parse_only_flag(only_price),
        only_manufacturer=_parse_only_flag(only_mfg),
    )
    html = _render_search_block_html(request, ctx)
    n = len(ctx["rows"]) if ctx["rows"] else 0
    return JSONResponse(
        {
            "html": html,
            "query_used": ctx["query_used"],
            "merge_note": ctx["merge_note"],
            "cache_note": ctx["cache_note"],
            "error": ctx["error"],
            "row_count": n,
            "row_count_unfiltered": ctx["row_count_unfiltered"],
        }
    )


@app.get("/export/search.csv", name="export_search_csv")
async def export_search_csv(
    request: Request,
    product: str = Query("", max_length=300),
    site_id: str = Query("all"),
    filter_site: str = Query("", max_length=32),
    name_contains: str = Query("", max_length=200),
    only_price: int = Query(0),
    only_mfg: int = Query(0),
) -> Response:
    ctx = await _build_search_block_context(
        request,
        product=product,
        site_id=site_id,
        filter_site=filter_site,
        name_contains=name_contains,
        only_price=_parse_only_flag(only_price),
        only_manufacturer=_parse_only_flag(only_mfg),
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "rating_stars",
            "rating",
            "name",
            "business_type",
            "region",
            "employees",
            "low_price",
            "source_site",
            "url",
            "platform_search_url",
            "audited",
        ]
    )
    for r in ctx["rows"] or []:
        w.writerow(
            [
                r.get("rating_stars", ""),
                r.get("rating", ""),
                r.get("name", ""),
                r.get("business_type", ""),
                r.get("region", ""),
                r.get("employees", ""),
                r.get("low_price", ""),
                r.get("source_site", ""),
                r.get("url", ""),
                r.get("platform_search_url", ""),
                "yes" if r.get("audited") else "",
            ]
        )
    data = buf.getvalue().encode("utf-8-sig")
    fname = "suppliers.csv"
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/search", response_class=HTMLResponse, name="search_page")
async def search_get(
    request: Request,
    product: str = Query(""),
    site_id: str = Query("all"),
    filter_site: str = Query("", max_length=32),
    name_contains: str = Query("", max_length=200),
    only_price: int = Query(0),
    only_mfg: int = Query(0),
) -> HTMLResponse:
    q = (product or "").strip()
    return await _render_search_page(
        request,
        q,
        site_id,
        filter_site=filter_site,
        name_contains=name_contains,
        only_price=_parse_only_flag(only_price),
        only_mfg=_parse_only_flag(only_mfg),
    )


@app.post("/search", response_model=None)
async def search_post(
    request: Request,
    product: str = Form(""),
    site_id: str = Form("all"),
):
    product = (product or "").strip()
    site_id = _normalize_site_id(site_id)
    if not product:
        return await _render_search_page(request, "", site_id)

    product_prepared = await prepare_query_for_platforms(product)
    loc = str(request.url_for("search_page"))
    loc = f"{loc}?{_export_query(product_prepared, site_id, '', '', False, False)}"
    return RedirectResponse(loc, status_code=303)
