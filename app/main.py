from __future__ import annotations

import asyncio
import base64
import csv
import os
import io
import logging
import re
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import File, FastAPI, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.platform_links import platform_search_urls
from app.query_translate import prepare_search_queries
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


async def _fetch_raw_rows(
    query_en: str, query_zh: str, site_id: str
) -> tuple[list[dict], str | None]:
    """query_en — MIC/Alibaba; query_zh — 1688, Taobao, Pinduoduo (иероглифы предпочтительнее)."""
    notes: list[str] = []
    merged: list[dict] = []
    q_intl = (query_en or "").strip()
    q_cn = (query_zh or query_en or "").strip() or q_intl

    if site_id == "mic":
        merged = await fetch_mic(q_intl, limit=35)
    elif site_id == "alibaba":
        merged = await fetch_alibaba(q_intl, limit=35)
    elif site_id == "1688":
        try:
            merged = await _wait_first(fetch_1688, q_cn, 30, 20.0, retries=1)
        except asyncio.TimeoutError:
            raise ValueError(
                "1688.com: нет ответа за отведённое время (часто недоступен вне Китая)."
            ) from None
    elif site_id == "taobao":
        try:
            merged = await _wait_first(fetch_taobao, q_cn, 30, 18.0, retries=1)
        except asyncio.TimeoutError:
            raise ValueError(
                "Taobao: нет ответа за отведённое время (часто недоступен вне Китая)."
            ) from None
    elif site_id == "pinduoduo":
        merged = await fetch_pinduoduo(q_cn, limit=30)
    elif site_id == "all":
        for fn, label, lim, tmo, use_cn in (
            (fetch_mic, "Made-in-China", 18, None, False),
            (fetch_alibaba, "Alibaba", 18, None, False),
            (fetch_1688, "1688", 8, 16.0, True),
            (fetch_taobao, "Taobao", 8, 14.0, True),
        ):
            q = q_cn if use_cn else q_intl
            try:
                if label in ("1688", "Taobao") and tmo is not None:
                    part = await _wait_first(fn, q, lim, tmo, retries=1)
                elif tmo is not None:
                    part = await asyncio.wait_for(
                        fn(q, limit=lim), timeout=tmo
                    )
                else:
                    part = await fn(q, limit=lim)
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
    cache_key: str,
    site_id: str,
    query_en: str,
    query_zh: str,
) -> tuple[list[dict], str | None, bool]:
    hit = await get_cached(site_id, cache_key)
    if hit is not None:
        rows, notes = hit
        return rows, notes, True
    rows, notes = await _fetch_raw_rows(query_en, query_zh, site_id)
    await set_cached(site_id, cache_key, rows, notes)
    return rows, notes, False


def _parse_only_flag(v: int | str) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "on")


_CN_QUERY_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"百褶|褶裙|半身裙|短裙|裙"), "pleated skirt"),
    (re.compile(r"松紧腰|高腰"), "high waist skirt"),
    (re.compile(r"口袋"), "skirt with pockets"),
    (re.compile(r"连衣裙"), "dress"),
    (re.compile(r"T恤|短袖|上衣"), "t shirt"),
    (re.compile(r"裤|短裤|牛仔"), "shorts"),
]

_BAD_LABEL_TOKENS = {
    "person",
    "human",
    "girl",
    "woman",
    "man",
    "skin",
    "leg",
    "thigh",
    "knee",
    "waist",
    "fashion model",
    "standing",
}

_PRODUCT_LABEL_HINTS = {
    "skirt",
    "pleated skirt",
    "mini skirt",
    "dress",
    "shirt",
    "t shirt",
    "t-shirt",
    "jacket",
    "hoodie",
    "coat",
    "pants",
    "shorts",
    "jeans",
    "sweater",
    "blouse",
    "bag",
    "backpack",
    "handbag",
    "shoes",
    "sneakers",
    "boot",
    "hat",
    "glasses",
    "sunglasses",
    "watch",
    "phone",
    "headphones",
    "laptop",
    "tablet",
    "toy",
    "furniture",
    "lamp",
}

# Явная одежда — чуть сильнее буст в скоринге.
_CLOTHING_LABEL_SUBSTR = (
    "skirt",
    "pleat",
    "miniskirt",
    "dress",
    "blouse",
    "sweater",
    "jacket",
    "hoodie",
    "coat",
    "cardigan",
    "apparel",
    "clothing",
    "outerwear",
    "legging",
    "jeans",
    "shorts",
)

# Любые узнаваемые товары (маркетплейс): не только одежда.
_PRODUCT_CATEGORY_SUBSTR = (
    # одежда / обувь / аксессуары
    "skirt",
    "pleat",
    "dress",
    "shirt",
    "jacket",
    "coat",
    "pants",
    "jean",
    "short",
    "shoe",
    "sneaker",
    "boot",
    "sandal",
    "bag",
    "backpack",
    "wallet",
    "belt",
    "hat",
    "cap",
    "scarf",
    "glove",
    "watch",
    "glasses",
    "sunglasses",
    "jewelry",
    "necklace",
    "ring",
    "earring",
    # электроника
    "phone",
    "smartphone",
    "tablet",
    "laptop",
    "computer",
    "headphone",
    "earphone",
    "earbud",
    "speaker",
    "charger",
    "cable",
    "camera",
    "keyboard",
    "mouse",
    "monitor",
    # дом / кухня
    "furniture",
    "chair",
    "table",
    "sofa",
    "lamp",
    "pillow",
    "blanket",
    "curtain",
    "kitchen",
    "pot",
    "pan",
    "bottle",
    "cup",
    "mug",
    # красота
    "cosmetic",
    "lipstick",
    "makeup",
    "perfume",
    "cream",
    # прочее
    "toy",
    "tool",
    "umbrella",
    "bicycle",
    "fitness",
    "yoga",
    "stroller",
)

# Ложные ходы именно для «карточки одежды» (носок вместо юбки и т.п.).
_APPAREL_CONFUSER_SUBSTR = (
    "sock",
    "stocking",
    "underwear",
    "lingerie",
    "bra",
    "tissue",
)

# Общие «левые» темы для снижения приоритета.
_NOISY_SUBSTR = (
    "fruit",
    "vegetable",
    "food",
    "motorcycle",
    "helmet",
    "vehicle",
    "animal",
    "pet",
)

_BAD_OCR_LINES = frozenset(
    {
        "袜",
        "袜子",
        "内裤",
        "内衣",
    }
)


def _score_label_for_query(raw: str) -> int:
    s = (raw or "").strip().lower()
    if not s or s in _BAD_LABEL_TOKENS:
        return -100
    score = 0
    if any(k in s for k in _PRODUCT_CATEGORY_SUBSTR):
        score += 6
    if any(k in s for k in _CLOTHING_LABEL_SUBSTR):
        score += 4
    if any(k in s for k in _APPAREL_CONFUSER_SUBSTR):
        score -= 5
    if any(k in s for k in _NOISY_SUBSTR):
        score -= 6
    if s in _PRODUCT_LABEL_HINTS:
        score += 3
    return score


def _best_label_query(labels: list) -> str | None:
    best_s = ""
    best_score = -999
    for it in labels if isinstance(labels, list) else []:
        if not isinstance(it, dict):
            continue
        raw = (it.get("description") or "").strip().lower()
        sc = _score_label_for_query(raw)
        if sc > best_score and raw:
            best_score = sc
            best_s = raw
    if best_score >= 3:
        return _normalize_product_query(best_s)
    return None


def _normalize_product_query(q: str) -> str:
    ql = (q or "").strip().lower()
    if not ql:
        return ""
    # Лёгкое уточнение для женской одежды на витрине (не одна фиксированная «юбка»).
    _women_apparel = (
        "skirt",
        "dress",
        "blouse",
        "sweater",
        "coat",
        "jacket",
        "hoodie",
        "cardigan",
        "jeans",
        "pants",
        "shorts",
        "legging",
        "top",
        "shirt",
        "t shirt",
        "t-shirt",
        "miniskirt",
        "pleat",
    )
    if any(k in ql for k in _women_apparel):
        if " women" not in ql and " men" not in ql and " kids" not in ql and " baby" not in ql:
            return (ql + " women")[:120]
    return ql[:120]


def _build_query_from_vision_response(resp: dict) -> str:
    text_ann = resp.get("textAnnotations") or []
    labels = resp.get("labelAnnotations") or []
    web_detection = resp.get("webDetection") or {}

    text_full = ""
    if isinstance(text_ann, list) and text_ann:
        text_full = "\n".join(
            [(x.get("description") or "").strip() for x in text_ann if isinstance(x, dict)]
        ).strip()

    # 1) Явные китайские подсказки в OCR-тексте (лучше всего для маркетплейсных картинок).
    if text_full:
        for rx, q in _CN_QUERY_HINTS:
            if rx.search(text_full):
                return _normalize_product_query(q)

    # 2) LABEL_DETECTION раньше WEB/OCR — реже путается с «носок / еда» на фото одежды.
    label_q = _best_label_query(labels)
    if label_q:
        return label_q

    # 3) WEB_DETECTION: пропускаем явные confuser-bestGuess.
    best_guess_labels = web_detection.get("bestGuessLabels") or []
    if isinstance(best_guess_labels, list) and best_guess_labels:
        best_guess = (best_guess_labels[0].get("label") or "").strip().lower()
        if best_guess and best_guess not in _BAD_LABEL_TOKENS:
            if _score_label_for_query(best_guess) < 0:
                pass
            else:
                return _normalize_product_query(best_guess)

    entities = web_detection.get("webEntities") or []
    if isinstance(entities, list):
        entity_names = []
        for ent in entities:
            if not isinstance(ent, dict):
                continue
            name = (ent.get("description") or "").strip().lower()
            if not name or name in _BAD_LABEL_TOKENS:
                continue
            entity_names.append(name)
        for name in sorted(
            entity_names, key=lambda s: _score_label_for_query(s), reverse=True
        ):
            if name in _PRODUCT_LABEL_HINTS or _score_label_for_query(name) >= 3:
                return _normalize_product_query(name)

    # 4) Остальные лейблы без высокого скора.
    cand: list[str] = []
    for it in labels if isinstance(labels, list) else []:
        raw = (it.get("description") or "").strip().lower()
        if not raw or raw in _BAD_LABEL_TOKENS:
            continue
        cand.append(raw)

    if cand:
        cand.sort(key=lambda s: _score_label_for_query(s), reverse=True)
        preferred = [c for c in cand if c in _PRODUCT_LABEL_HINTS]
        if preferred:
            return _normalize_product_query(preferred[0])
        if _score_label_for_query(cand[0]) >= 0:
            return _normalize_product_query(cand[0])

    # 5) OCR: не брать одну китайскую букву типа «袜» и известный мусор.
    if text_full:
        for line in text_full.splitlines():
            t = line.strip()
            if not t or len(t) > 120:
                continue
            if t in _BAD_OCR_LINES:
                continue
            if len(t) == 1 and "\u4e00" <= t <= "\u9fff":
                continue
            if any(c in t for c in ("袜",)) and "裙" not in t and "裤" not in t:
                continue
            return _normalize_product_query(t)
    return ""


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
    query_zh = ""

    product_stripped = (product or "").strip()
    if not product_stripped:
        return {
            "rows": None,
            "row_count_unfiltered": 0,
            "query_used": "",
            "query_zh": "",
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
        query_used, query_zh = await prepare_search_queries(product_stripped)
        cache_key = f"{query_used}\x1e{query_zh}"
        raw, merge_note, from_cache = await _load_rows(
            cache_key, site_id, query_used, query_zh
        )
        if from_cache:
            cache_note = "Показан кэш сырой выдачи (~15 мин), без повторного запроса к площадкам."

        scored = score_rows(raw)
        row_count_unfiltered = len(scored)
        urls = platform_search_urls(query_used, query_zh)
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
        "query_zh": query_zh,
        "platform_urls": (
            platform_search_urls(query_used, query_zh) if query_used else {}
        ),
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
                "rows", "row_count_unfiltered", "query_used", "query_zh", "platform_urls",
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
                "rows", "row_count_unfiltered", "query_used", "query_zh", "platform_urls",
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
        return JSONResponse({"en": "", "zh": "", "changed": False})
    en, zh = await prepare_search_queries(raw)
    changed = en != raw or zh != raw
    return JSONResponse({"en": en, "zh": zh, "changed": changed})


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
                    {"type": "LABEL_DETECTION", "maxResults": 15},
                    {"type": "TEXT_DETECTION", "maxResults": 1},
                    {"type": "WEB_DETECTION", "maxResults": 10},
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
    query = _build_query_from_vision_response(resp)
    logger.warning("vision_query_selected query=%s", query)

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
    try:
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
                "query_zh": ctx.get("query_zh", ""),
                "merge_note": ctx["merge_note"],
                "cache_note": ctx["cache_note"],
                "error": ctx["error"],
                "row_count": n,
                "row_count_unfiltered": ctx["row_count_unfiltered"],
            }
        )
    except Exception as e:
        logger.exception("api_search_result_failed: %s", e)
        return JSONResponse(
            {
                "html": '<div class="alert">Ошибка поиска: внутренняя ошибка обработки результата.</div>',
                "query_used": "",
                "query_zh": "",
                "merge_note": None,
                "cache_note": None,
                "error": f"Внутренняя ошибка: {e!s}",
                "row_count": 0,
                "row_count_unfiltered": 0,
            },
            status_code=200,
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

    loc = str(request.url_for("search_page"))
    loc = f"{loc}?{_export_query(product, site_id, '', '', False, False)}"
    return RedirectResponse(loc, status_code=303)
