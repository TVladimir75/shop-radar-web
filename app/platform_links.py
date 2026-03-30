"""Прямые ссылки на страницу поиска на каждой площадке (по подготовленному запросу)."""

from __future__ import annotations

from urllib.parse import quote_plus

from app.scrapers.common import b2b_path_slug, quote_path_segment

SOURCE_TO_KEY = {
    "Made-in-China": "MIC",
    "Alibaba": "Alibaba",
    "1688": "1688",
    "Taobao": "Taobao",
    "AliExpress": "AliExpress",
    "Pinduoduo": "Pinduoduo",
}


def platform_search_urls(prepared_query: str) -> dict[str, str]:
    q = (prepared_query or "").strip() or "LED"
    slug = b2b_path_slug(q)
    enc = quote_path_segment(slug)
    qp = quote_plus(q)
    return {
        "MIC": f"https://www.made-in-china.com/manufacturers/{enc}.html",
        "Alibaba": f"https://www.alibaba.com/showroom/{enc}.html",
        "1688": f"https://s.1688.com/selloffer/offer_search.htm?keywords={qp}",
        "Taobao": f"https://s.taobao.com/search?commend=all&ie=utf8&q={qp}",
        "AliExpress": f"https://www.aliexpress.com/wholesale?SearchText={qp}",
        "Pinduoduo": f"https://mobile.yangkeduo.com/search_result.h?keyword={qp}",
    }


def row_platform_search_url(source_site: str, urls: dict[str, str]) -> str:
    k = SOURCE_TO_KEY.get(source_site or "", "MIC")
    return urls.get(k, urls.get("MIC", ""))
