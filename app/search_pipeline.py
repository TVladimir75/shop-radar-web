"""Скоринг, фильтры, обогащение строк для выдачи."""

from __future__ import annotations

from typing import Any

from app.platform_links import row_platform_search_url
from app.scoring import score_row, score_to_stars


def _sort_float(x: Any, default: float = 0.0) -> float:
    if x is None or x == "":
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def result_sort_key(row: dict) -> tuple:
    rating = _sort_float(row.get("rating"), 0.0)
    p = row.get("price_usd_min")
    price_part = p if p is not None else float("inf")
    stars = row.get("mic_stars") or 0
    cs = row.get("mic_cs_level") or 0
    txn_part = _sort_float(row.get("alibaba_txn_level"), 0.0)
    try:
        stars = int(stars)
    except (TypeError, ValueError):
        stars = 0
    try:
        cs = int(cs)
    except (TypeError, ValueError):
        cs = 0
    return (-rating, price_part, -stars, -cs, -txn_part)


def normalize_row(r: dict) -> dict:
    x = dict(r)
    x.setdefault("low_price", "")
    x.setdefault("mic_stars", None)
    x.setdefault("mic_cs_level", None)
    x.setdefault("alibaba_txn_level", None)
    x.setdefault("alibaba_gold", False)
    x.setdefault("price_usd_min", None)
    x.setdefault("source_site", "")
    return x


def score_rows(raw: list[dict]) -> list[dict]:
    scored: list[dict] = []
    for r in raw:
        r = normalize_row(r)
        sc = score_row(
            has_audited=r["audited"],
            business_type=r.get("business_type"),
            has_region=bool(r.get("region")),
            has_employees=bool(r.get("employees")),
            name_len=len(r["name"]),
        )
        rs = max(1, min(5, int(score_to_stars(sc))))
        scored.append({**r, "rating": float(sc), "rating_stars": rs})
    scored.sort(key=result_sort_key)
    return scored


def attach_platform_links(rows: list[dict], urls: dict[str, str]) -> None:
    for r in rows:
        r["platform_search_url"] = row_platform_search_url(r.get("source_site", ""), urls)


FILTER_LABEL_TO_SOURCE = {
    "": "",
    "mic": "Made-in-China",
    "alibaba": "Alibaba",
    "1688": "1688",
    "taobao": "Taobao",
    "pinduoduo": "Pinduoduo",
}


def filter_scored(
    rows: list[dict],
    *,
    filter_site_key: str,
    name_contains: str,
    only_with_price: bool,
    only_manufacturer: bool,
) -> list[dict]:
    src = FILTER_LABEL_TO_SOURCE.get((filter_site_key or "").lower().strip(), "")
    name_need = (name_contains or "").strip().lower()
    out: list[dict] = []
    for r in rows:
        if src and (r.get("source_site") or "") != src:
            continue
        if name_need:
            blob = f"{r.get('name', '')} {r.get('snippet', '')}".lower()
            if name_need not in blob:
                continue
        if only_with_price:
            if not (r.get("low_price") or r.get("price_usd_min") is not None):
                continue
        if only_manufacturer:
            bt = (r.get("business_type") or "").lower()
            if "manufacturer" not in bt and "factory" not in bt:
                continue
        out.append(r)
    return out
