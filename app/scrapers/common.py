"""Общие утилиты для парсеров."""

from __future__ import annotations

import json
import re
from urllib.parse import quote

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


# Русские запросы → ключевые слова для URL Made-in-China / Alibaba (латиница).
# Длинные фразы выше — чтобы «солнцезащитные очки» не стало только «очки».
_RU_EN_PHRASES: tuple[tuple[str, str], ...] = (
    ("нижнее бельё", "underwear"),
    ("нижнее белье", "underwear"),
    ("зимняя куртка", "winter jacket"),
    ("кожаная куртка", "leather jacket"),
    ("солнечная панель", "solar panel"),
    ("солнечные панели", "solar panel"),
    ("солнцезащитные очки", "sunglasses"),
    ("очки для чтения", "reading glasses"),
    ("оправа для очков", "eyeglass frames"),
    ("оправы для очков", "eyeglass frames"),
    ("очки", "eyeglasses"),
    ("оправа", "eyeglass frames"),
    ("контактные линзы", "contact lenses"),
    ("наручные часы", "wrist watch"),
    ("часы наручные", "wrist watch"),
    ("наушники", "headphones"),
    ("беспроводные наушники", "wireless headphones"),
    ("зарядка телефона", "phone charger"),
    ("смартфон", "smartphone"),
    ("мобильный телефон", "mobile phone"),
    ("ноутбук", "laptop"),
    ("монитор", "computer monitor"),
    ("клавиатура", "keyboard"),
    ("компьютерная мышь", "computer mouse"),
    ("мышь компьютерная", "computer mouse"),
    ("принтер", "printer"),
    ("кабель hdmi", "hdmi cable"),
    ("перчатки рабочие", "work gloves"),
    ("рабочие перчатки", "work gloves"),
    ("перчатки зимние", "winter gloves"),
    ("перчатки", "gloves"),
    ("респиратор", "respirator mask"),
    ("маска медицинская", "medical mask"),
    ("одноразовая маска", "disposable face mask"),
    ("ботинки", "boots"),
    ("кроссовки", "sneakers"),
    ("куртка", "jacket"),
    ("пуховик", "down jacket"),
    ("ветровка", "windbreaker"),
    ("пальто", "wool coat"),
    ("плащ", "raincoat"),
    ("платье", "dress"),
    ("футболка", "t-shirt"),
    ("рубашка", "shirt"),
    ("брюки", "trousers"),
    ("джинсы", "jeans"),
    ("юбка", "skirt"),
    ("шапка", "winter hat"),
    ("шарф", "scarf"),
    ("носки", "socks"),
    ("крем для лица", "facial cream"),
    ("помада", "lipstick"),
    ("шампунь", "shampoo"),
    ("рюкзак", "backpack"),
    ("зонт", "umbrella"),
    ("чемодан", "luggage suitcase"),
    ("ковёр", "carpet rug"),
    ("ковер", "carpet rug"),
    ("шторы", "curtain"),
    ("постельное бельё", "bedding set"),
    ("постельное белье", "bedding set"),
    ("подушка", "pillow"),
    ("одеяло", "blanket"),
    ("сумка женская", "handbag"),
    ("сумка", "hand bag"),
    ("ремень", "leather belt"),
    ("украшения", "fashion jewelry"),
    ("бижутерия", "costume jewelry"),
    ("мебельная фурнитура", "furniture hardware"),
    ("мягкая мебель", "upholstery furniture"),
    ("диван", "sofa"),
    ("диваны", "sofa"),
    ("мебель", "home furniture"),
    ("обеденный стол", "dining table"),
    ("письменный стол", "office desk"),
)


def normalize_product_query_for_slug(query: str) -> str:
    """
    MIC/Alibaba строят URL только из латиницы; чистая кириллица превращалась в slug «product»
    и выдача становилась случайной (крепёж, оборудование и т.д.).
    Подставляем известные RU→EN фрагменты; смешанный запрос с латиницей не трогаем.
    """
    q = (query or "").strip()
    if not q:
        return q
    if slugify_alnum(q) != "product":
        return q
    out = q.lower().strip()
    for ru, en in _RU_EN_PHRASES:
        if ru in out:
            out = out.replace(ru, en)
    out = re.sub(r"\s+", " ", out).strip(" -")
    return out or q


_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")


def b2b_path_slug(product_query: str) -> str:
    """
    Сегмент пути для MIC /showroom и Alibaba (без «product», если можно избежать).
    1) Словарь RU→EN + латиница → eyeglasses / led / …
    2) Иначе кириллица с дефисами («очки» → узкая выдача по оптике, а не общий /product/).
    """
    raw = (product_query or "").strip()
    if not raw:
        return "product"
    normalized = normalize_product_query_for_slug(raw)
    slug = slugify_alnum(normalized)
    if slug != "product":
        return slug
    if _CYRILLIC_RE.search(raw):
        seg = re.sub(r"[^\w\-]+", "-", raw.strip(), flags=re.UNICODE)
        seg = re.sub(r"-+", "-", seg).strip("-").lower()
        if seg:
            return seg
    return "product"


def quote_path_segment(segment: str, *, safe: str = "-_") -> str:
    """Кодирует сегмент пути (кириллица → %…), оставляя дефис и подчёркивание."""
    return quote(segment, safe=safe)


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
