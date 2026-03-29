from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.scoring import score_row, score_to_stars
from app.scrapers.alibaba_scraper import fetch_suppliers as fetch_alibaba
from app.scrapers.cn1688 import fetch_suppliers as fetch_1688
from app.scrapers.mic import fetch_suppliers as fetch_mic
from app.scrapers.taobao_scraper import fetch_suppliers as fetch_taobao
from app.query_translate import prepare_query_for_platforms
from app.site_meta import footer_context, landing_context, tool_href
from app.suggestions import suggest as suggest_products

BASE = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


def _page_ctx(request: Request, **kwargs):
    base = str(request.base_url).rstrip("/")
    return {**footer_context(), "page_base_url": base, **kwargs}

SITES = [
    {"id": "all", "label": "Все: MIC + Alibaba + 1688 + Taobao", "active": True},
    {"id": "mic", "label": "Made-in-China.com", "active": True},
    {"id": "alibaba", "label": "Alibaba.com (showroom)", "active": True},
    {"id": "1688", "label": "1688.com", "active": True},
    {"id": "taobao", "label": "Taobao.com", "active": True},
]

_SITE_IDS = {s["id"] for s in SITES}


def _normalize_site_id(site_id: str) -> str:
    s = (site_id or "all").strip()
    return s if s in _SITE_IDS else "all"


async def _render_search_page(
    request: Request, product: str, site_id: str
) -> HTMLResponse:
    site_id = _normalize_site_id(site_id)
    error: str | None = None
    rows: list[dict] | None = None
    merge_note: str | None = None

    if not product:
        pass
    else:
        try:
            raw, merge_note = await _load_rows(product, site_id)
            scored: list[dict] = []
            for r in raw:
                r = _normalize_row(r)
                sc = score_row(
                    has_audited=r["audited"],
                    business_type=r.get("business_type"),
                    has_region=bool(r.get("region")),
                    has_employees=bool(r.get("employees")),
                    name_len=len(r["name"]),
                )
                rs = max(1, min(5, int(score_to_stars(sc))))
                scored.append(
                    {**r, "rating": float(sc), "rating_stars": rs}
                )
            scored.sort(key=_result_sort_key)
            rows = scored
            if not rows:
                error = (
                    "Ничего не найдено. Попробуйте другие слова; на MIC/Alibaba на английском обычно лучше."
                )
        except ValueError as e:
            error = str(e)
        except Exception as e:
            error = f"Не удалось загрузить данные: {e!s}"

    return templates.TemplateResponse(
        request,
        "index.html",
        _page_ctx(
            request,
            sites=SITES,
            rows=rows,
            query=product,
            site_id=site_id,
            error=error,
            merge_note=merge_note,
        ),
    )


def _normalize_row(r: dict) -> dict:
    """Все ключи для шаблона (иначе Jinja даёт «пустые» поля и обрезанный текст)."""
    x = dict(r)
    x.setdefault("low_price", "")
    x.setdefault("mic_stars", None)
    x.setdefault("mic_cs_level", None)
    x.setdefault("alibaba_txn_level", None)
    x.setdefault("alibaba_gold", False)
    x.setdefault("price_usd_min", None)
    x.setdefault("source_site", "")
    return x


def _sort_float(x, default: float = 0.0) -> float:
    if x is None or x == "":
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _result_sort_key(row: dict):
    """Рейтинг → цена (USD) → MIC-звёзды / уровень → уровень сделок Alibaba."""
    rating = _sort_float(row.get("rating"), 0.0)
    p = row.get("price_usd_min")
    price_part = p if p is not None else float("inf")
    stars = row.get("mic_stars") or 0
    cs = row.get("mic_cs_level") or 0
    txn_part = _sort_float(row.get("alibaba_txn_level"), 0.0)
    try:
        stars = int(stars)
    except (TypeError, ValueError):
        stars = 0
    try:
        cs = int(cs)
    except (TypeError, ValueError):
        cs = 0
    return (-rating, price_part, -stars, -cs, -txn_part)


async def _load_rows(product: str, site_id: str) -> tuple[list[dict], str | None]:
    """Возвращает сырые строки и необязательное примечание (для режима «все»)."""
    notes: list[str] = []
    merged: list[dict] = []

    if site_id == "mic":
        merged = await fetch_mic(product, limit=35)
    elif site_id == "alibaba":
        merged = await fetch_alibaba(product, limit=35)
    elif site_id == "1688":
        merged = await fetch_1688(product, limit=30)
    elif site_id == "taobao":
        merged = await fetch_taobao(product, limit=30)
    elif site_id == "all":
        for fn, label, lim, tmo in (
            (fetch_mic, "Made-in-China", 18, None),
            (fetch_alibaba, "Alibaba", 18, None),
            (fetch_1688, "1688", 8, 16.0),
            (fetch_taobao, "Taobao", 8, 14.0),
        ):
            try:
                if tmo is not None:
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


app = FastAPI(title="Подбор поставщиков")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request) -> HTMLResponse:
    """Лицевая страница; инструмент поиска — /tool."""
    tool_rel = str(request.url_for("tool_home"))
    ctx = {**landing_context(), "tool_url": tool_href(tool_rel)}
    return templates.TemplateResponse(request, "landing.html", ctx)


@app.get("/tool", response_class=HTMLResponse, name="tool_home")
async def tool_home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        _page_ctx(
            request,
            sites=SITES,
            rows=None,
            query="",
            site_id="all",
            error=None,
            merge_note=None,
        ),
    )


@app.get("/api/suggest")
async def api_suggest(q: str = Query("", max_length=120)) -> JSONResponse:
    """Подсказки при наборе в поле «товар / тема»."""
    items = suggest_products(q, limit=12)
    return JSONResponse({"items": items})


@app.get("/api/translate_query")
async def api_translate_query(q: str = Query("", max_length=300)) -> JSONResponse:
    """Подстановка EN в строку поиска после паузы при вводе по-русски."""
    raw = (q or "").strip()
    if not raw:
        return JSONResponse({"en": "", "changed": False})
    en = await prepare_query_for_platforms(raw)
    changed = en != raw
    return JSONResponse({"en": en, "changed": changed})


@app.get("/search", response_class=HTMLResponse, name="search_page")
async def search_get(
    request: Request,
    product: str = Query(""),
    site_id: str = Query("all"),
) -> HTMLResponse:
    """Тот же поиск по URL (обновление страницы после POST больше не даёт 404)."""
    q = (product or "").strip()
    if q:
        q = await prepare_query_for_platforms(q)
    return await _render_search_page(request, q, site_id)


@app.post("/search", response_model=None)
async def search_post(
    request: Request,
    product: str = Form(""),
    site_id: str = Form("all"),
):
    """
    POST → редирект на GET с параметрами (PRG): в адресной строке /search?...,
    повторное открытие и обновление не шлют POST без тела и не ломаются.
    """
    product = (product or "").strip()
    site_id = _normalize_site_id(site_id)
    if not product:
        return await _render_search_page(request, "", site_id)

    product = await prepare_query_for_platforms(product)

    loc = str(request.url_for("search_page"))
    loc = f"{loc}?{urlencode({'product': product, 'site_id': site_id})}"
    return RedirectResponse(loc, status_code=303)
