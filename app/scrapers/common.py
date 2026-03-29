"""Общие утилиты для парсеров."""

from __future__ import annotations

import json
import re

CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": CHROME_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def slugify_alnum(text: str) -> str:
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "product"


def extract_json_assignment(html: str, var_name: str) -> dict | None:
    """Выдёргивает присваивание вида window.var = {...}; с учётом вложенных скобок."""
    needle = f"{var_name} = "
    i = html.find(needle)
    if i < 0:
        return None
    start = i + len(needle)
    depth = 0
    in_str = False
    esc = False
    quote = ""
    for j in range(start, len(html)):
        c = html[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                in_str = False
            continue
        if c in "\"'":
            in_str = True
            quote = c
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start : j + 1])
                except json.JSONDecodeError:
                    return None
    return None


def parse_usd_min_from_text(s: str | None) -> float | None:
    """US$ / US $ / USD / «голый» $ в строке цены."""
    if not s:
        return None
    m = (
        re.search(r"US\s*\$\s*([\d,]+(?:\.\d+)?)", s, re.I)
        or re.search(r"USD\s*([\d,]+(?:\.\d+)?)", s, re.I)
        or re.search(r"\$\s*([\d,]+(?:\.\d+)?)", s)
    )
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None
