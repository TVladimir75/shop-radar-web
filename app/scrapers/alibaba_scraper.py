"""Alibaba: витрина showroom (SSR JSON window._PAGE_DATA_)."""

from __future__ import annotations

from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.scrapers.common import (
    BROWSER_HEADERS,
    b2b_path_slug,
    extract_json_assignment,
    quote_path_segment,
)
from app.scrapers.common import parse_usd_min_from_text as parse_usd_min

ALIBABA_BASE = "https://www.alibaba.com"


async def fetch_suppliers(product_query: str, limit: int = 25) -> list[dict]:
    slug = b2b_path_slug(product_query)
    enc = quote_path_segment(slug)
    url = f"{ALIBABA_BASE}/showroom/{enc}.html"
    async with httpx.AsyncClient(
        timeout=45.0,
        follow_redirects=True,
        headers=BROWSER_HEADERS,
    ) as client:
        r = await client.get(url)
        r.raise_for_status()
        try:
            soup = BeautifulSoup(r.text, "lxml")
        except Exception:
            soup = BeautifulSoup(r.text, "html.parser")

    data = extract_json_assignment(r.text, "window._PAGE_DATA_")
    if not data:
        return []

    offer_block = data.get("offerResultData") or {}
    items = offer_block.get("itemInfoList") or []
    out: list[dict] = []
    seen: set[str] = set()

    for wrap in items:
        if len(out) >= limit:
            break
        offer = (wrap or {}).get("offer")
        if not isinstance(offer, dict):
            continue
        sup = offer.get("supplier") or {}
        name = (sup.get("supplierName") or "").strip()
        href = (sup.get("supplierHref") or "").strip()
        if not name or not href:
            continue
        abs_h = urljoin(ALIBABA_BASE + "/", href)
        p = urlparse(abs_h)
        scheme = p.scheme or "https"
        home = f"{scheme}://{p.netloc}/"

        if home in seen:
            continue
        seen.add(home)

        info = offer.get("information") or {}
        snippet = (info.get("pureTitle") or info.get("enPureTitle") or "")[:400]

        trade = offer.get("tradePrice") or {}
        price_txt = (
            (trade.get("priceMini") or trade.get("price") or "").strip()
            or (offer.get("lowerPrice") or "").strip()
        )
        low_price = ""
        if price_txt:
            low_price = price_txt if price_txt.lower().startswith("us") else f"от {price_txt}"

        comp = offer.get("company") or {}
        txn = comp.get("transactionLevelFloat")
        try:
            txn_f = float(txn) if txn is not None else None
        except (TypeError, ValueError):
            txn_f = None

        gold = bool(sup.get("goldSupplier"))
        years = sup.get("supplierYear")
        audited = gold or bool(sup.get("assessedSupplier"))

        products = (sup.get("provideProducts") or "").strip()
        bt = "Trading Company"
        if products and "manufacturer" in products.lower():
            bt = "Manufacturer"

        country = (sup.get("supplierCountry") or {}).get("name") or ""

        out.append(
            {
                "name": name,
                "url": home,
                "business_type": bt,
                "region": country,
                "employees": f"{years} yrs on site" if years else "",
                "audited": audited,
                "snippet": snippet,
                "low_price": low_price,
                "mic_stars": None,
                "mic_cs_level": None,
                "price_usd_min": parse_usd_min(low_price) or parse_usd_min(price_txt),
                "alibaba_txn_level": txn_f,
                "alibaba_gold": gold,
                "source_site": "Alibaba",
            }
        )

    if not out and soup.select("punish-component"):
        raise RuntimeError(
            "Alibaba вернул страницу проверки (captcha). Повторите позже или смените сеть."
        )
    return out
