"""Pinduoduo: мобильная выдача (mobile.yangkeduo.com) через Playwright.

Парсер сделан максимально «мягким»:
- сначала грузим страницу headless;
- затем пробуем вытащить карточки товара по ссылкам `goods2.html?goods_id=...`;
- цену пытаемся найти эвристически рядом с ссылкой (символ `¥`/`￥` в локальном тексте).

Без residential proxy успех может быть не 100% — в таком случае поднимем RuntimeError,
чтобы на UI показать понятную ошибку (а не падение приложения).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from typing import Any
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from app.scrapers.common import CHROME_UA, BROWSER_HEADERS, b2b_path_slug, quote_path_segment


PINDUODUO_SEARCH_BASE = "https://mobile.yangkeduo.com/search_result.h"

_PW_INSTALL_LOCK = asyncio.Lock()
_PW_INSTALL_DONE = False


_GOODS_RE = re.compile(r"goods2\.html\?goods_id=(\d+)", re.I)
_PRICE_RE = re.compile(r"(?:¥|￥)\s*([\d]+(?:\.[\d]+)?)")


def _parse_price_from_blob(blob: str) -> str:
    m = _PRICE_RE.search(blob or "")
    if not m:
        return ""
    try:
        val = float(m.group(1))
    except ValueError:
        return ""
    # округляем мягко — дальше сортировка работает по numeric price_usd_min
    if val.is_integer():
        return f"¥{int(val)}"
    return f"¥{val:g}"


def _price_to_usd_min(low_price: str | None) -> float | None:
    if not low_price:
        return None
    m = _PRICE_RE.search(low_price)
    if not m:
        return None
    try:
        yuan = float(m.group(1))
    except ValueError:
        return None
    # курс упрощённый, как в других местах проекта
    return yuan / 7.2


def _normalize_name(s: str, *, max_len: int = 90) -> str:
    t = (s or "").strip()
    t = re.sub(r"\s+", " ", t)
    if not t:
        return ""
    if len(t) <= max_len:
        return t
    return t[: max_len - 1].rstrip() + "…"


def _unescape_json_str(s: str) -> str:
    try:
        # Значение лежит как JSON-строка внутри HTML, поэтому декодируем через json.loads.
        # Например: \u4e2d\u6587, \\n и т.п.
        return json.loads(f'"{s}"')
    except Exception:
        try:
            return s.encode("utf-8").decode("unicode_escape")
        except Exception:
            return s


async def fetch_suppliers(product_query: str, limit: int = 25) -> list[dict[str, Any]]:
    q = (product_query or "").strip()
    if not q:
        q = "LED"
    # Render часто не передаёт PLAYWRIGHT_BROWSERS_PATH в рантайм, поэтому
    # задаём его явно, чтобы Playwright искал бинарник в одном и том же месте.
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/render/cache/ms-playwright")
    # Pinduoduo мобильная выдача обычно любит латиницу/англ. slug-подход как в других.
    slug = b2b_path_slug(q)
    keyword = quote_plus(slug)
    url = f"{PINDUODUO_SEARCH_BASE}?keyword={keyword}"

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception as e:
            msg = str(e)
            # На Render иногда забывают шаг `playwright install`, поэтому бинарника нет.
            if "Executable doesn't exist" in msg or "chrome-headless-shell" in msg:
                async with _PW_INSTALL_LOCK:
                    global _PW_INSTALL_DONE
                    if not _PW_INSTALL_DONE:
                        browsers_path = os.environ.get(
                            "PLAYWRIGHT_BROWSERS_PATH", "/opt/render/cache/ms-playwright"
                        )
                        env = os.environ.copy()
                        env["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path
                        subprocess.run(
                            [
                                sys.executable,
                                "-m",
                                "playwright",
                                "install",
                                "chromium",
                            ],
                            check=True,
                            env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        _PW_INSTALL_DONE = True
                browser = await p.chromium.launch(headless=True)
            else:
                raise
        try:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent=CHROME_UA,
                locale="en-US",
            )
            page = await context.new_page()
            # Немного увеличиваем таймаут — мобильные страницы иногда грузят дольше.
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1800)
            final_url = page.url
            title = await page.title()
            html = await page.content()
        finally:
            await browser.close()

    low = (html or "").lower()
    # Детекторы антибота должны быть узкими, иначе получаем ложные срабатывания на служебные слова в JS.
    # Поэтому опираемся в первую очередь на title и явные ключевые фразы для капчи.
    if (
        "captcha" in low
        or "капча" in low
        or "滑动" in html
        or "滑块" in html
        or ("captcha" in (title or "").lower())
        or ("验证码" in (title or ""))
        or ("验证" in (title or ""))
    ):
        raise RuntimeError(
            "Pinduoduo показал проверку/капчу (captcha/验证码). Попробуйте позже/другую сеть."
        )
    if "短信" in html:
        # Если SMS встречается в тексте страницы — это скорее всего реальная верификация.
        raise RuntimeError("Pinduoduo запрашивает подтверждение (SMS/верификация).")

    # Выдача — React/SPA: карточки товара лежат в embedded JSON внутри HTML.
    # Вокруг `goods_id=...` обычно встречаются `goods_name`, `price` и `link_url`.
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    id_matches = list(re.finditer(r"goods_id=(\d+)", html))
    for m in id_matches:
        gid = m.group(1)
        if gid in seen:
            continue
        seen.add(gid)

        idx = m.start()
        win = html[max(0, idx - 900) : idx + 1800]

        name_m = re.search(r'"goods_name"\s*:\s*"([^"]+)"', win)
        price_m = re.search(r'"price"\s*:\s*([0-9]+)', win)
        link_m = re.search(
            r'"link_url"\s*:\s*"([^"]*goods_id=' + re.escape(gid) + r'[^"]*)"',
            win,
        )

        if not name_m:
            continue

        name_raw = _normalize_name(_unescape_json_str(name_m.group(1)))
        if not name_raw:
            continue

        low_price = ""
        price_usd_min: float | None = None
        if price_m:
            try:
                # Pinduoduo `price` в поиске обычно в копейках (fen).
                fen = int(price_m.group(1))
                yuan = fen / 100.0
                low_price = f"¥{yuan:g}"
                price_usd_min = yuan / 7.2
            except ValueError:
                pass

        link_url = link_m.group(1) if link_m else ""
        url_out = (
            urljoin("https://mobile.yangkeduo.com", link_url) if link_url else url
        )

        out.append(
            {
                "name": name_raw,
                "url": url_out,
                "business_type": "Seller",
                "region": "China",
                "employees": "",
                "audited": False,
                "snippet": name_raw[:400],
                "low_price": low_price,
                "mic_stars": None,
                "mic_cs_level": None,
                "alibaba_txn_level": None,
                "alibaba_gold": False,
                "price_usd_min": price_usd_min,
                "source_site": "Pinduoduo",
            }
        )

        if len(out) >= limit:
            break

    if not out:
        # Важно: не использовать финальный URL в сообщении (он может быть длинный),
        # но сигнализировать «почему» пользователю/себе.
        raise RuntimeError(
            "Pinduoduo: не удалось извлечь карточки товара (нет goods_name/price в HTML)."
        )

    return out

