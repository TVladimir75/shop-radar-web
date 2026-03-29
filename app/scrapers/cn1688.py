"""1688.com — поиск поставщиков (доступность сильно зависит от сети)."""

from __future__ import annotations

import re
from urllib.parse import quote_plus, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from app.scrapers.common import BROWSER_HEADERS, parse_usd_min_from_text

SEARCH_URL = "https://s.1688.com/selloffer/offer_search.htm"


def _company_home(href: str) -> str:
    href = (href or "").strip()
    if not href or href.startswith("javascript:"):
        return ""
    if href.startswith("//"):
        href = "https:" + href
    p = urlparse(href)
    if not p.netloc:
        return ""
    return urlunparse((p.scheme or "https", p.netloc, "/", "", "", ""))


async def fetch_suppliers(product_query: str, limit: int = 25) -> list[dict]:
    q = (product_query or "").strip() or "LED"
    url = f"{SEARCH_URL}?keywords={quote_plus(q)}"
    headers = {
        **BROWSER_HEADERS,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.1688.com/",
    }
    try:
        async with httpx.AsyncClient(
            timeout=22.0,
            follow_redirects=True,
            headers=headers,
        ) as client:
            r = await client.get(url)
    except httpx.TimeoutException:
        raise RuntimeError(
            "1688 не ответил вовремя. Сайт часто недоступен или сильно тормозит вне Китая — "
            "используйте Alibaba / Made-in-China или попробуйте другую сеть."
        ) from None

    if r.status_code != 200 or len(r.text) < 1500:
        raise RuntimeError(
            "1688 вернул пустую или служебную страницу (ограничение, капча). "
            "Откройте 1688 в обычном браузере в той же сети и повторите."
        )

    text = r.text
    if "punish" in text.lower() or "captcha" in text.lower() or "滑动" in text:
        raise RuntimeError(
            "1688 запросил проверку (капча/антибот). Обойти это из приложения нельзя — "
            "выберите Alibaba или Made-in-China."
        )

    try:
        soup = BeautifulSoup(text, "lxml")
    except Exception:
        soup = BeautifulSoup(text, "html.parser")

    out: list[dict] = []
    seen: set[str] = set()

    # Несколько поколений вёрстки: заголовок ссылки на оффер + цена рядом
    for row in soup.select("div.offer-item, li.offer-list-item, .space-item"):
        if len(out) >= limit:
            break
        title_a = row.select_one("a[href*='offer'], a[href*='detail.1688.com']")
        if not title_a:
            continue
        title = title_a.get_text(" ", strip=True)
        if not title or len(title) < 6:
            continue
        shop_a = row.select_one("a[href*='shop'], a.company-name, .company-name a")
        name = ""
        home = ""
        if shop_a and shop_a.get("href"):
            cand = shop_a.get_text(" ", strip=True)
            if len(cand) > 2:
                name = cand
            home = _company_home(shop_a.get("href") or "")
        if not home:
            home = _company_home(title_a.get("href") or "")
        if not name:
            name = title[:80]

        price_el = row.select_one(".price, .price-value, [class*='price']")
        price_raw = ""
        if price_el:
            price_raw = price_el.get_text(" ", strip=True)
        if not price_raw:
            m = re.search(
                r"(¥\s*[\d.,]+\s*(?:[-–]\s*¥?\s*[\d.,]+)?)", row.get_text(" ", strip=True)
            )
            if m:
                price_raw = m.group(1)
        low_price = f"от {price_raw}" if price_raw else ""
        yuan = None
        if price_raw:
            ym = re.search(r"¥\s*([\d,]+(?:\.\d+)?)", price_raw)
            if ym:
                try:
                    yuan = float(ym.group(1).replace(",", ""))
                except ValueError:
                    yuan = None

        if home and home in seen:
            continue
        if home:
            seen.add(home)

        out.append(
            {
                "name": name.strip(),
                "url": home or (_company_home(title_a.get("href") or "") or url),
                "business_type": None,
                "region": "China",
                "employees": "",
                "audited": False,
                "snippet": title[:400],
                "low_price": low_price,
                "mic_stars": None,
                "mic_cs_level": None,
                "alibaba_txn_level": None,
                "alibaba_gold": None,
                "price_usd_min": (yuan / 7.2) if yuan else parse_usd_min_from_text(low_price),
                "source_site": "1688",
            }
        )

    if not out:
        raise RuntimeError(
            "1688: не удалось разобрать выдачу (сайт изменил вёрстку или отдал пустую страницу). "
            "Попробуйте Alibaba / Made-in-China."
        )
    return out
