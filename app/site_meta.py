"""Подвал «Developed by…» — можно переопределить переменными окружения."""

from __future__ import annotations

import os

_DEV_NAME = (os.environ.get("SHOP_RADAR_DEV_NAME") or "Vladimir Tatarinov").strip()
_TG_HANDLE = (os.environ.get("SHOP_RADAR_TELEGRAM_HANDLE") or "TVladimir75").strip().lstrip("@")
TELEGRAM_URL = (
    os.environ.get("SHOP_RADAR_TELEGRAM") or f"https://t.me/{_TG_HANDLE}"
).strip()

# Telegram-бот (нижняя ссылка в подвале)
_BOT = (os.environ.get("SHOP_RADAR_TELEGRAM_BOT") or "vt_kz_bot").strip().lstrip("@")
BOT_URL = (os.environ.get("SHOP_RADAR_TELEGRAM_BOT_URL") or f"https://t.me/{_BOT}").strip()

# Абсолютный URL приложения-поиска (если лендинг на другом домене, например Render)
_TOOL_BASE = (os.environ.get("SHOP_RADAR_TOOL_BASE") or "").strip().rstrip("/")
# Сайт в подвале лендинга
_BRAND_SITE = (os.environ.get("SHOP_RADAR_BRAND_URL") or "https://vt.com.kz").strip().rstrip("/")


def tool_href(request_url_for_tool: str) -> str:
    """Ссылка на инструмент: внешняя база или относительный /tool на этом же хосте."""
    return _TOOL_BASE or request_url_for_tool


def landing_context() -> dict:
    return {
        **footer_context(),
        "brand_site_url": _BRAND_SITE,
    }


def footer_context() -> dict:
    return {
        "footer_intro": "Developed by",
        "footer_dev_name": _DEV_NAME,
        "footer_telegram_handle": _TG_HANDLE,
        "footer_telegram_url": TELEGRAM_URL,
        "footer_bot_handle": _BOT,
        "footer_bot_url": BOT_URL,
    }
