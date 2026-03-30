"""Кэш сырой выдачи парсеров (TTL), чтобы не дёргать площадки повторно."""

from __future__ import annotations

import asyncio
import time
from typing import Any

CACHE_TTL_SEC = 900.0
_LOCK = asyncio.Lock()
_STORE: dict[tuple[str, str], tuple[float, tuple[list[dict[str, Any]], str | None]]] = {}


async def get_cached(site_id: str, product_key: str) -> tuple[list[dict], str | None] | None:
    key = (site_id, product_key)
    async with _LOCK:
        now = time.monotonic()
        hit = _STORE.get(key)
        if not hit:
            return None
        ts, payload = hit
        if now - ts >= CACHE_TTL_SEC:
            del _STORE[key]
            return None
        return payload


async def set_cached(
    site_id: str, product_key: str, rows: list[dict], notes: str | None
) -> None:
    key = (site_id, product_key)
    async with _LOCK:
        _STORE[key] = (time.monotonic(), (rows, notes))
        while len(_STORE) > 400:
            _STORE.pop(next(iter(_STORE)))
