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


def platform_search_urls(query_en: str, query_zh: str | None = None) -> dict[str, str]:
    """MIC/Alibaba — латиница; 1688 / Taobao / Pinduoduo — китайский запрос, если передан."""
    q_en = (query_en or "").strip() or "LED"
    q_zh = ((query_zh if query_zh is not None else query_en) or "").strip() or q_en
    slug_en = b2b_path_slug(q_en)
    enc_en = quote_path_segment(slug_en)
    qp_en = quote_plus(q_en)
    qp_zh = quote_plus(q_zh)
    return {
        "MIC": f"https://www.made-in-china.com/manufacturers/{enc_en}.html",
        "Alibaba": f"https://www.alibaba.com/showroom/{enc_en}.html",
        "1688": f"https://s.1688.com/selloffer/offer_search.htm?keywords={qp_zh}",
        "Taobao": f"https://s.taobao.com/search?commend=all&ie=utf8&q={qp_zh}",
        "AliExpress": f"https://www.aliexpress.com/wholesale?SearchText={qp_en}",
        # .html стабильнее для открытия в браузере, чем .h (реже ведёт на главную 推荐).
        "Pinduoduo": f"https://mobile.yangkeduo.com/search_result.html?keyword={qp_zh}",
    }


def row_platform_search_url(source_site: str, urls: dict[str, str]) -> str:
    k = SOURCE_TO_KEY.get(source_site or "", "MIC")
    return urls.get(k, urls.get("MIC", ""))
