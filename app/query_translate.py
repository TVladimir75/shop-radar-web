"""Перевод поискового запроса RU→EN для MIC / Alibaba (и смешанного ввода)."""

from __future__ import annotations

import re
from collections import OrderedDict
from urllib.parse import quote

import httpx

from app.scrapers.common import (
    CJK_RE,
    en_to_zh_from_dictionary,
    normalize_product_query_for_slug,
)

_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
_CACHE: OrderedDict[str, str] = OrderedDict()
_CACHE_MAX = 256
_ZH_FROM_EN_CACHE: OrderedDict[str, str] = OrderedDict()
_ZH_CACHE_MAX = 256


def _cache_get(key: str) -> str | None:
    val = _CACHE.get(key)
    if val is not None:
        _CACHE.move_to_end(key)
    return val


def _cache_put(key: str, value: str) -> None:
    _CACHE[key] = value
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)


async def _lingva_ru_en(text: str) -> str | None:
    """
    Бесплатный прокси к Google Translate (публичный инстанс).
    Список зеркал: https://github.com/thedaviddelta/lingva-translate
    """
    t = text.strip()
    if not t or len(t) > 380:
        return None
    try:
        path = quote(t, safe="")
        url = f"https://lingva.ml/api/v1/ru/en/{path}"
        async with httpx.AsyncClient(timeout=14.0, follow_redirects=True) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return None
            data = r.json()
        out = (data.get("translation") or "").strip()
        return out or None
    except Exception:
        return None


async def _mymemory_ru_en(text: str) -> str | None:
    """Запасной канал (https://mymemory.translated.net/)."""
    t = text.strip()
    if not t or len(t) > 450:
        return None
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.get(
                "https://api.mymemory.translated.net/get",
                params={"q": t, "langpair": "ru|en"},
            )
            r.raise_for_status()
            data = r.json()
        out = (data.get("responseData") or {}).get("translatedText") or ""
        out = out.strip()
        if not out or "MYMEMORY" in out.upper():
            return None
        return out
    except Exception:
        return None


async def _online_ru_en(text: str) -> str:
    t = text.strip()
    if not t:
        return text
    cached = _cache_get(t)
    if cached is not None:
        return cached
    out = await _lingva_ru_en(t)
    if not out:
        out = await _mymemory_ru_en(t)
    if not out or out.lower() == t.lower():
        return text
    _cache_put(t, out)
    return out


def _zh_cache_get(key: str) -> str | None:
    val = _ZH_FROM_EN_CACHE.get(key)
    if val is not None:
        _ZH_FROM_EN_CACHE.move_to_end(key)
    return val


def _zh_cache_put(key: str, value: str) -> None:
    _ZH_FROM_EN_CACHE[key] = value
    _ZH_FROM_EN_CACHE.move_to_end(key)
    while len(_ZH_FROM_EN_CACHE) > _ZH_CACHE_MAX:
        _ZH_FROM_EN_CACHE.popitem(last=False)


async def _lingva_en_zh(text: str) -> str | None:
    t = text.strip()
    if not t or len(t) > 380:
        return None
    try:
        path = quote(t, safe="")
        url = f"https://lingva.ml/api/v1/en/zh/{path}"
        async with httpx.AsyncClient(timeout=14.0, follow_redirects=True) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return None
            data = r.json()
        out = (data.get("translation") or "").strip()
        return out or None
    except Exception:
        return None


async def _mymemory_en_zh(text: str) -> str | None:
    t = text.strip()
    if not t or len(t) > 450:
        return None
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.get(
                "https://api.mymemory.translated.net/get",
                params={"q": t, "langpair": "en|zh-CN"},
            )
            r.raise_for_status()
            data = r.json()
        out = (data.get("responseData") or {}).get("translatedText") or ""
        out = out.strip()
        if not out or "MYMEMORY" in out.upper():
            return None
        return out
    except Exception:
        return None


async def _online_en_zh(text: str) -> str:
    """EN или латинский запрос → упрощённый китайский (для внутренних маркетплейсов)."""
    t = text.strip()
    if not t:
        return t
    hit = _zh_cache_get(t)
    if hit is not None:
        return hit
    out = await _lingva_en_zh(t)
    if not out:
        out = await _mymemory_en_zh(t)
    if not out or out.lower() == t.lower():
        return text
    _zh_cache_put(t, out)
    return out


async def augment_query_zh_from_en(query_en: str) -> str:
    """
    Китайские площадки лучше понимают иероглифы в keywords= / q=.
    Если в строке уже есть CJK — не трогаем; иначе словарь, затем онлайн EN→ZH.
    """
    s = (query_en or "").strip()
    if not s:
        return s
    if CJK_RE.search(s):
        return s
    zh = en_to_zh_from_dictionary(s)
    if zh:
        return zh
    return await _online_en_zh(s)


async def prepare_search_queries(raw: str) -> tuple[str, str]:
    """
    Единая подготовка для всего поиска (ручной ввод и тот же текст после Vision):
    - query_en — MIC / Alibaba / ссылки витрин;
    - query_zh — 1688, Taobao, Pinduoduo (и совпадает с query_en, если уже китайский).
    """
    raw = (raw or "").strip()
    if not raw:
        return "", ""
    query_en = await prepare_query_for_platforms(raw)
    query_zh = await augment_query_zh_from_en(query_en)
    return query_en.strip(), (query_zh or query_en).strip()


def has_cyrillic(text: str) -> bool:
    return bool(_CYRILLIC_RE.search(text or ""))


async def prepare_query_for_platforms(raw: str) -> str:
    """
    1) Локальный словарь в normalize_product_query_for_slug.
    2) Если осталась кириллица — перевод в английский для URL/витрин.
    """
    s = (raw or "").strip()
    if not s:
        return s
    n = normalize_product_query_for_slug(s)
    if not _CYRILLIC_RE.search(n):
        return n.strip()
    tr = await _online_ru_en(n)
    return (tr or n).strip() or n
