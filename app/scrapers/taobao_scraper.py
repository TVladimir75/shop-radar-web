"""Taobao (s.taobao.com) — выдача по поиску; сильный антибот, часто только из Китая / браузера."""

from __future__ import annotations

import re
from urllib.parse import quote_plus, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from app.scrapers.common import (
    BROWSER_HEADERS,
    extract_json_assignment,
    normalize_product_query_for_slug,
    parse_usd_min_from_text,
)

SEARCH_BASE = "https://s.taobao.com/search"


def _shop_home(href: str) -> str:
    href = (href or "").strip()
    if not href or href.startswith("javascript:"):
        return ""
    if href.startswith("//"):
        href = "https:" + href
    p = urlparse(href)
    if not p.netloc:
        return ""
    return urlunparse((p.scheme or "https", p.netloc, "/", "", "", ""))


def _abs(base: str, href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    return urljoin(base, href)


def _auctions_from_g_page_config(cfg: dict) -> list[dict]:
    if not isinstance(cfg, dict):
        return []
    mods = cfg.get("mods") or {}
    for block_key in ("itemlist", "grid", "vertical", "item_list"):
        block = mods.get(block_key)
        if not isinstance(block, dict):
            continue
        data = block.get("data") or {}
        for auc_key in ("auctions", "items", "collections"):
            raw = data.get(auc_key)
            if isinstance(raw, list) and raw:
                return [x for x in raw if isinstance(x, dict)]
    return []


def _item_to_row(item: dict, search_url: str) -> dict | None:
    title = (
        (item.get("raw_title") or item.get("title") or item.get("short_title") or "")
        .replace("<span class=H>", "")
        .replace("</span>", "")
    )
    title = re.sub(r"<[^>]+>", "", title).strip()
    if not title or len(title) < 4:
        return None

    nick = (item.get("nick") or item.get("sellerNick") or "").strip()
    shopcard = item.get("shopcard") or {}
    if isinstance(shopcard, dict):
        nick = nick or (shopcard.get("sellerNick") or "").strip()

    shop_link = (item.get("shop_url") or item.get("shopLink") or "").strip()
    detail = (
        item.get("detail_url")
        or item.get("auctionURL")
        or item.get("url")
        or ""
    )
    detail = _abs("https://www.taobao.com/", detail)
    home = _shop_home(shop_link) if shop_link else _shop_home(detail)
    name = nick or (title[:60] + "…" if len(title) > 60 else title)
    if not home and detail:
        home = _shop_home(detail)

    view_price = (
        item.get("view_price")
        or item.get("viewPrice")
        or item.get("price")
        or item.get("reserve_price")
        or ""
    )
    view_price = str(view_price).strip()
    low_price = f"¥ {view_price}" if view_price else ""
    yuan = None
    if view_price:
        try:
            yuan = float(re.sub(r"[^\d.]", "", view_price.split("-")[0]))
        except ValueError:
            yuan = None

    url_out = home or detail or search_url
    return {
        "name": name,
        "url": url_out,
        "business_type": "Seller",
        "region": (item.get("item_loc") or item.get("procity") or "China"),
        "employees": "",
        "audited": False,
        "snippet": title[:400],
        "low_price": low_price,
        "mic_stars": None,
        "mic_cs_level": None,
        "alibaba_txn_level": None,
        "alibaba_gold": None,
        "price_usd_min": (yuan / 7.2) if yuan else parse_usd_min_from_text(low_price),
        "source_site": "Taobao",
    }


async def fetch_suppliers(product_query: str, limit: int = 25) -> list[dict]:
    q = normalize_product_query_for_slug((product_query or "").strip()) or "LED"
    url = f"{SEARCH_BASE}?commend=all&ie=utf8&q={quote_plus(q)}"
    headers = {
        **BROWSER_HEADERS,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.taobao.com/",
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
            "Taobao не ответил вовремя. Попробуйте позже или другую сеть "
            "(сайт часто доступен лучше из Китая)."
        ) from None

    text = r.text
    if r.status_code != 200 or len(text) < 2000:
        raise RuntimeError(
            "Taobao вернул пустую или служебную страницу (блокировка по IP или региону)."
        )

    low = text.lower()
    if "punish" in low or "captcha" in low or "滑动" in text or "login.taobao" in low:
        raise RuntimeError(
            "Taobao запросил проверку или вход (антибот). Из облачного сервера часто недоступно — "
            "используйте 1688, Alibaba или Made-in-China."
        )

    cfg = extract_json_assignment(text, "g_page_config")
    auctions = _auctions_from_g_page_config(cfg) if cfg else []

    if not auctions:
        # запасной разбор ссылок на карточки (старая/упрощённая вёрстка)
        try:
            soup = BeautifulSoup(text, "lxml")
        except Exception:
            soup = BeautifulSoup(text, "html.parser")
        auctions = []
        for a in soup.select("a[href*='item.taobao.com'], a[href*='detail.tmall.com']"):
            href = a.get("href") or ""
            t = a.get_text(" ", strip=True)
            if href and len(t) > 6:
                auctions.append(
                    {
                        "raw_title": t,
                        "detail_url": href,
                        "nick": "",
                    }
                )
            if len(auctions) >= limit * 2:
                break

    if not auctions:
        raise RuntimeError(
            "Taobao: не удалось разобрать выдачу (изменилась вёрстка, нет JSON или пустая страница). "
            "Чаще работает из сетей ближе к Китаю."
        )

    out: list[dict] = []
    seen: set[str] = set()
    for item in auctions:
        if len(out) >= limit:
            break
        row = _item_to_row(item, url)
        if not row:
            continue
        key = row["url"].split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(row)

    if not out:
        raise RuntimeError(
            "Taobao: в ответе не оказалось строк для таблицы. Попробуйте другой запрос или площадку."
        )
    return out
