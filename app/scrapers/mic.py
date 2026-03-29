"""Made-in-China: список поставщиков по slug из ключевых слов."""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
import httpx

from app.scrapers.common import BROWSER_HEADERS, parse_usd_min_from_text
from app.scrapers.common import slugify_alnum as slugify

MIC_BASE = "https://www.made-in-china.com"
HEADERS = BROWSER_HEADERS


def listing_url(slug: str, page: int) -> str:
    if page <= 1:
        return f"{MIC_BASE}/manufacturers/{slug}.html"
    return f"{MIC_BASE}/manufacturers/{slug}_{page}.html"


def abs_url(href: str) -> str:
    href = (href or "").strip()
    if not href or href.startswith("javascript:"):
        return ""
    return urljoin(MIC_BASE + "/", href)


def store_home(href: str) -> str:
    u = abs_url(href)
    if not u:
        return ""
    p = urlparse(u)
    if p.netloc.endswith(".made-in-china.com"):
        return f"{p.scheme}://{p.netloc}/"
    return u


def intro_field(node, needle: str) -> str | None:
    for lab in node.select(".company-intro label.subject"):
        if needle.lower() in lab.get_text(strip=True).lower():
            parent = lab.parent
            if not parent:
                continue
            sp = parent.select_one("span")
            if sp:
                t = (sp.get("title") or sp.get_text() or "").strip()
                return t or None
    return None


def _parse_usd_prices(text: str) -> list[float]:
    """Достаёт числа из US$ / USD / $ (в тексте блоков цен)."""
    text = (
        text.replace("\xa0", " ")
        .replace("\u2009", " ")
        .replace("&nbsp;", " ")
    )
    out: list[float] = []
    patterns = (
        r"US\s*\$\s*([\d,]+(?:\.\d+)?)\s*(?:[-–]\s*([\d,]+(?:\.\d+)?))?",
        r"USD\s*([\d,]+(?:\.\d+)?)\s*(?:[-–]\s*([\d,]+(?:\.\d+)?))?",
        r"(?<![A-Z€£¥])\$\s*([\d,]+(?:\.\d+)?)\s*(?:[-–]\s*([\d,]+(?:\.\d+)?))?",
    )
    for pat in patterns:
        for m in re.finditer(pat, text, re.I):
            try:
                lo = float(m.group(1).replace(",", ""))
                out.append(lo)
                if m.lastindex and m.lastindex >= 2 and m.group(2):
                    out.append(float(m.group(2).replace(",", "")))
            except (ValueError, IndexError):
                continue
    return out


def lowest_price_hint(node) -> str:
    """
    Минимальная цена среди показанных на карточке товаров.
    На MIC (2024+) цены в div.prd-price[title] и strong.price, не в span.price.
    """
    chunks: list[str] = []
    for el in node.select("div.prd-price, a.prod-price, a.prod-price .price"):
        tit = (el.get("title") or "").strip()
        if tit:
            chunks.append(tit)
        strong = el.select_one("strong.price, span.price")
        if strong:
            chunks.append(strong.get_text(" ", strip=True))
    # старая вёрстка / запасной путь
    for el in node.select("span.price, strong.price"):
        chunks.append(el.get_text(" ", strip=True))
        if el.get("title"):
            chunks.append(el["title"])
    wrap = node.select_one(".rec-product-wrap, ul.rec-product")
    if wrap and not chunks:
        chunks.append(wrap.get_text(" ", strip=True))
    combined = " ".join(chunks)
    for a in node.select(".rec-product a[title], ul.rec-product li a"):
        t = (a.get("title") or "").strip()
        if "US$" in t:
            combined += " " + t
    prices = _parse_usd_prices(combined)
    if not prices:
        # любые US$ в тексте карточки (редкий шаблон без div.prd-price)
        prices = _parse_usd_prices(node.get_text(" ", strip=True))
    if not prices:
        return ""
    lo = min(prices)
    if lo >= 1000:
        return f"от US$ {lo:,.0f}".replace(",", " ")
    s = f"{lo:.4f}".rstrip("0").rstrip(".")
    return f"от US$ {s}"


def mic_stars_hint(node) -> int | None:
    """Индекс возможностей на MIC: число звёзд (1–5), если блок есть в карточке."""
    span = node.select_one(
        "span.auth-icon-item.icon-star, span.icon-star.J-tooltip-ele, span.icon-star"
    )
    if not span:
        return None
    n = len(span.select('img[src*="star-light"]'))
    return n if 1 <= n <= 5 else None


def mic_cs_level_hint(node) -> int | None:
    """
    Уровень возможностей из встроенного JSON (на выдаче list-node часто есть только он).
    Обычно 30 или 50 — чем выше, тем выше уровень по данным MIC.
    """
    for scr in node.select('script[type="application/json"].J-video-json'):
        raw = scr.string
        if not raw:
            continue
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
        v = data.get("csLevel")
        if v is None or v == "":
            continue
        try:
            n = int(str(v).strip())
        except ValueError:
            continue
        return n if n > 0 else None
    return None


def employee_hint(node) -> str | None:
    for span in node.select("span.auth-block.basic-ability"):
        img = span.select_one("img")
        alt = (img.get("alt") or "") if img else ""
        if "employee" not in alt.lower():
            continue
        raw = span.get_text(strip=True)
        m = re.search(r"\d[\d,\-]*", raw)
        if m:
            return m.group(0).replace(" ", "")
    return None


async def fetch_suppliers(product_query: str, limit: int = 25) -> list[dict]:
    slug = slugify(product_query)
    out: list[dict] = []
    seen: set[str] = set()
    async with httpx.AsyncClient(
        timeout=45.0,
        follow_redirects=True,
        headers=HEADERS,
    ) as client:
        page = 1
        while len(out) < limit and page <= 8:
            url = listing_url(slug, page)
            r = await client.get(url)
            r.raise_for_status()
            try:
                soup = BeautifulSoup(r.text, "lxml")
            except Exception:
                soup = BeautifulSoup(r.text, "html.parser")
            added = 0
            for node in soup.select("div.list-node"):
                if len(out) >= limit:
                    break
                a = node.select_one("h2.company-name a.company-name-link")
                if not a:
                    continue
                name = a.get_text(strip=True)
                home = store_home(a.get("href") or "")
                if not name or not home or home in seen:
                    continue
                seen.add(home)
                bt = intro_field(node, "Business Type")
                region = intro_field(node, "City/Province")
                emp = employee_hint(node)
                audited = bool(
                    node.select_one("img.ico-audited, span .ico-audited")
                )
                snippet_el = node.select_one(".company-description .content")
                snippet = (
                    snippet_el.get_text(" ", strip=True)[:400]
                    if snippet_el
                    else ""
                )
                low_price = lowest_price_hint(node)
                stars = mic_stars_hint(node)
                cs_level = mic_cs_level_hint(node)
                out.append(
                    {
                        "name": name,
                        "url": home,
                        "business_type": bt,
                        "region": region or "",
                        "employees": emp or "",
                        "audited": audited,
                        "snippet": snippet,
                        "low_price": low_price,
                        "mic_stars": stars,
                        "mic_cs_level": cs_level,
                        "alibaba_txn_level": None,
                        "alibaba_gold": None,
                        "price_usd_min": parse_usd_min_from_text(low_price),
                        "source_site": "Made-in-China",
                    }
                )
                added += 1
            if added == 0:
                break
            page += 1
    return out
