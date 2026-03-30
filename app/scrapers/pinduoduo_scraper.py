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
from urllib.parse import parse_qs, quote_plus, unquote_plus, urlparse

from playwright.async_api import async_playwright

from app.scrapers.common import CJK_RE, b2b_path_slug, normalize_product_query_for_slug


PINDUODUO_SEARCH_BASE = "https://mobile.yangkeduo.com/search_result.h"
# Запасной вариант — иногда .h редиректит на главную, а .html держит выдачу.
PINDUODUO_SEARCH_ALT = "https://mobile.yangkeduo.com/search_result.html"

# Настоящий мобильный UA: меньше шансов получить «главную/рекламу» вместо блока выдачи.
PINDUODUO_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# Токены запроса (латиница) → типичные иероглифы в названии карточки на PDD.
_QUERY_EN_CN: tuple[tuple[str, str], ...] = (
    ("skirt", "裙"),
    ("pleat", "百褶"),
    ("dress", "连衣裙"),
    ("shirt", "衫"),
    ("coat", "外套"),
    ("jacket", "夹克"),
    ("pant", "裤"),
    ("jean", "牛仔"),
    ("shoe", "鞋"),
    ("boot", "靴"),
    ("sandal", "凉鞋"),
    ("bag", "包"),
    ("watch", "手表"),
    ("glass", "眼镜"),
    ("sunglass", "墨镜"),
    ("phone", "手机"),
    ("headphone", "耳机"),
    ("laptop", "笔记本"),
    ("tablet", "平板"),
    ("led", "灯"),
    ("lamp", "灯"),
)

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


def _looks_like_search_url(url: str) -> bool:
    """Реальная страница поиска, а не главная «推荐» без keyword."""
    if not url:
        return False
    u = url.lower()
    if "search_result" in u:
        return True
    try:
        qs = parse_qs(urlparse(url).query)
        if qs.get("keyword") or qs.get("search_key"):
            return True
    except Exception:
        pass
    return False


def _url_keyword_equals(url: str, qn: str) -> bool:
    try:
        qs = parse_qs(urlparse(url).query)
        raw = (qs.get("keyword") or qs.get("search_key") or [""])[0]
        if not raw:
            return False
        return unquote_plus(raw).strip() == (qn or "").strip()
    except Exception:
        return False


def _query_echoed_in_page_html(html: str, qn: str) -> bool:
    """На настоящей выдаче запрос почти всегда встречается в JSON/HTML; на главной 推荐 — нет."""
    if not html or not (qn or "").strip():
        return True
    q = (qn or "").strip()
    needles: list[str] = [q, quote_plus(q)]
    try:
        needles.append(json.dumps(q, ensure_ascii=True)[1:-1])
    except Exception:
        pass
    for n in needles:
        if n and n in html:
            return True
    if not CJK_RE.search(q) and len(q) >= 4:
        try:
            if re.search(rf"(?<![a-z0-9]){re.escape(q)}(?![a-z0-9])", html, re.I):
                return True
        except re.error:
            pass
    return False


def _tokens_from_query(q: str, slug: str) -> set[str]:
    """Слова из запроса и из slug (дефисы → слова)."""
    raw = f"{q} {slug.replace('-', ' ')}"
    raw = raw.lower().strip()
    parts = re.split(r"[^a-z0-9\u4e00-\u9fff]+", raw)
    out: set[str] = set()
    for p in parts:
        if len(p) >= 2 and not p.isdigit():
            out.add(p)
        # китайский — часто односложно
        if len(p) == 1 and "\u4e00" <= p <= "\u9fff":
            out.add(p)
    return out


def _relevance_score(
    name: str,
    tokens: set[str],
    q_lower: str,
    *,
    query_display: str = "",
) -> int:
    """Токены запроса + EN→CN подсказки; для 中文 — явное вхождение фразы без регекса.

    Односимвольные китайские подсказки (鞋、裙、灯…) дают меньший вес — иначе
    вложенные иероглифы в чужих словах слишком часто дают ложное совпадение.
    """
    if not name:
        return 0
    nl = name.lower()
    score = 0
    qcjk = re.sub(r"[\s\-]+", "", (query_display or "").strip())
    if len(qcjk) >= 2 and CJK_RE.search(qcjk):
        nn = re.sub(r"\s+", "", name)
        if qcjk in nn:
            score += 32
        else:
            # Частичное совпадение (网球, 球裙) — названия редко дословно «网球裙».
            seen_bg: set[str] = set()
            for i in range(len(qcjk) - 1):
                bg = qcjk[i : i + 2]
                if bg in seen_bg or not CJK_RE.search(bg):
                    continue
                seen_bg.add(bg)
                if bg in nn:
                    score += 9
    for t in tokens:
        if len(t) < 2:
            continue
        try:
            if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", nl, re.I):
                score += 5
        except re.error:
            if t in nl:
                score += 4
        if t in name:
            score += 1
    for en, cn in _QUERY_EN_CN:
        try:
            q_hit = bool(re.search(rf"(?<![a-z]){re.escape(en)}(?![a-z])", q_lower))
        except re.error:
            q_hit = en in q_lower
        tok_hit = any(
            re.search(rf"(?<![a-z0-9]){re.escape(en)}(?![a-z0-9])", x, re.I)
            for x in tokens
            if len(en) <= len(x)
        )
        if q_hit or tok_hit:
            if cn in name:
                score += 3 if len(cn) == 1 else 6
    return max(0, score)


async def fetch_suppliers(
    product_query: str,
    limit: int = 25,
    *,
    latin_hint: str = "",
) -> list[dict[str, Any]]:
    q = (product_query or "").strip()
    if not q:
        q = "LED"
    # Render часто не передаёт PLAYWRIGHT_BROWSERS_PATH в рантайм, поэтому
    # задаём его явно, чтобы Playwright искал бинарник в одном и том же месте.
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/render/cache/ms-playwright")
    # Латиницу кодируем как slug; чистый китайский/смешанный — целиком в keyword (иначе «product»).
    qn = normalize_product_query_for_slug(q).strip() or q
    slug = b2b_path_slug(qn)
    if CJK_RE.search(qn) or slug == "product":
        keyword = quote_plus(qn)
    else:
        keyword = quote_plus(slug)
    url = f"{PINDUODUO_SEARCH_BASE}?keyword={keyword}"
    slug_tok = slug if slug != "product" else ""
    tokens: set[str] = set(_tokens_from_query(qn, slug_tok))
    hint = (latin_hint or "").strip()
    if hint and hint != qn:
        hs = b2b_path_slug(hint)
        tokens |= set(_tokens_from_query(hint, hs if hs != "product" else ""))
    q_lower = f"{qn} {hint}".lower().strip()

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
        final_url = ""
        html = ""
        title = ""
        try:
            context = await browser.new_context(
                viewport={"width": 390, "height": 844},
                user_agent=PINDUODUO_MOBILE_UA,
                locale="zh-CN",
            )
            page = await context.new_page()
            # Немного увеличиваем таймаут — мобильные страницы иногда грузят дольше.
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_load_state("networkidle", timeout=9000)
            except Exception:
                pass
            await page.wait_for_timeout(2400)
            final_url = page.url
            # Частый кейс: search_result.h уводит на главную — тогда пробуем .html?keyword=
            if not _looks_like_search_url(final_url):
                alt = f"{PINDUODUO_SEARCH_ALT}?keyword={keyword}"
                await page.goto(alt, wait_until="domcontentloaded", timeout=45000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=9000)
                except Exception:
                    pass
                await page.wait_for_timeout(2400)
                final_url = page.url
            try:
                for depth in (1600, 3200, 4800):
                    await page.evaluate(
                        f"() => window.scrollTo(0, Math.min(document.body.scrollHeight, {depth}))"
                    )
                    await page.wait_for_timeout(900)
            except Exception:
                pass
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

    if not _looks_like_search_url(final_url):
        raise RuntimeError(
            "Pinduoduo: открылась не страница поиска (часто редирект на главную с вкладкой «推荐»). "
            "Сервер вне Китая/без сессии так видит сайт — выдача будет случайной. "
            "Попробуйте текстовый запрос точнее или другую сеть."
        )

    # В HTML много goods_id (реклама, рекомендации). Собираем кандидатов и
    # сортируем по релевантности к запросу — иначе в таблице «случайный мусор».
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    id_matches = list(re.finditer(r"goods_id=(\d+)", html))[:260]
    for m in id_matches:
        gid = m.group(1)
        if gid in seen:
            continue
        seen.add(gid)

        idx = m.start()
        win = html[max(0, idx - 900) : idx + 1800]

        name_m = re.search(r'"goods_name"\s*:\s*"([^"]+)"', win)
        price_m = re.search(r'"price"\s*:\s*([0-9]+)', win)

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

        # goods2.html часто требует вход. «Сайт» ведёт в поиск по тому же запросу, что и скрапер
        # (не по названию карточки — при ленте 推荐 карточка может быть «стельки» и ссылка уводит не туда).
        url_out = f"{PINDUODUO_SEARCH_ALT}?keyword={keyword}"

        rel = _relevance_score(name_raw, tokens, q_lower, query_display=qn)
        candidates.append(
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
                "_rel": rel,
            }
        )

    if not candidates:
        # Важно: не использовать финальный URL в сообщении (он может быть длинный),
        # но сигнализировать «почему» пользователю/себе.
        raise RuntimeError(
            "Pinduoduo: не удалось извлечь карточки товара (нет goods_name/price в HTML)."
        )

    max_rel = max(c["_rel"] for c in candidates)
    # Нет ни одной релевантной карточки — типично лента 推荐 с посторонними товарами.
    if max_rel <= 0:
        echoed = _query_echoed_in_page_html(html, qn)
        if (
            not echoed
            and _url_keyword_equals(final_url, qn)
            and len(candidates) >= 8
        ):
            raise RuntimeError(
                "Pinduoduo: открылась главная с вкладкой «推荐», а не поиск по запросу "
                "(в адресе есть keyword, но в разметке нет ваших слов — как на вашем скриншоте). "
                "С дата-центра/без китайского IP сайт часто подсовывает общую ленту. "
                "Откройте ссылку «Pinduoduo» внизу через VPN к КНР или в приложении."
            )
        if len(candidates) >= 12 and echoed:
            warn = (
                "Совпадений с запросом мало, но это похоже на страницу поиска. "
                "Ниже первые карточки из выдачи — проверьте формулировку. "
            )
            out_fb: list[dict[str, Any]] = []
            for c in candidates[:limit]:
                c.pop("_rel", None)
                sn = (c.get("snippet") or c.get("name") or "")[:360]
                c["snippet"] = warn + sn
                out_fb.append(c)
            return out_fb
        raise RuntimeError(
            "Pinduoduo: в разметке нет карточек, похожих на запрос (часто лента «推荐», а не поиск). "
            "Уточните текст (китайский/английский) или попробуйте сеть ближе к Китаю."
        )
    floor = 1
    if max_rel >= 6:
        floor = max(2, min(max_rel // 2, max_rel - 1))
    filtered = [c for c in candidates if c["_rel"] >= floor]
    if len(filtered) < min(3, max(1, len(candidates) // 4)):
        filtered = [c for c in candidates if c["_rel"] >= 1]
    if not filtered:
        raise RuntimeError(
            "Pinduoduo: после отбора по релевантности не осталось позиций — запрос слишком общий или выдача «не та»."
        )
    candidates = filtered

    candidates.sort(
        key=lambda c: (
            -int(c["_rel"]),
            -1 if c.get("low_price") else 0,
        )
    )
    out: list[dict[str, Any]] = []
    for c in candidates[:limit]:
        c.pop("_rel", None)
        out.append(c)

    return out

