"""
Пилот headless-проверки доступности выдачи для AliExpress и Pinduoduo.

Цель: быстро понять, что видит сайт при загрузке страниц поиска (captcha/redirect/пусто/данные),
без residential proxy и без попыток полноценного скрапинга.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from playwright.async_api import async_playwright

from app.platform_links import platform_search_urls
from app.query_translate import prepare_query_for_platforms
from app.suggestions import suggest as suggest_products


@dataclass(frozen=True)
class PilotResult:
    platform: str
    query_raw: str
    query_prepared: str
    requested_url: str
    final_url: str
    title: str
    html_len: int
    signals: list[str]


_SIGNAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("captcha", re.compile(r"captcha|капча|驗證碼|滑动|滑塊|verify", re.I)),
    ("login", re.compile(r"login|вход|log in|请登录|登录|sign in", re.I)),
    ("sms", re.compile(r"sms|短信|验证码|verification code|код", re.I)),
    ("robots", re.compile(r"robot|bot|бот|访问过于频繁|too many", re.I)),
    ("empty_or_error", re.compile(r"нет результатов|no results|пусто|error|异常|系统", re.I)),
]


def _detect_signals(html: str) -> list[str]:
    out: list[str] = []
    for label, pat in _SIGNAL_PATTERNS:
        if pat.search(html):
            out.append(label)
    return out


async def _load_page(page, url: str) -> tuple[str, str, int, str]:
    """
    Загружает страницу и возвращает: final_url, title, html_len, html_snippet.
    """
    resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    # Немного подождём подгрузку SPA/рендером (если она есть).
    await page.wait_for_timeout(1500)
    final_url = page.url
    title = await page.title()
    html = await page.content()
    # Снижаем размер вывода: оставляем короткий сниппет.
    snippet = html[:2000]
    _ = resp  # resp полезен для отладки, но не для пилота
    return final_url, title, len(html), snippet


async def _run_one(
    *,
    browser,
    platform: str,
    query_raw: str,
):
    prepared = await prepare_query_for_platforms(query_raw)
    urls = platform_search_urls(prepared)
    requested_url = urls.get(platform, "")
    if not requested_url:
        return PilotResult(
            platform=platform,
            query_raw=query_raw,
            query_prepared=prepared,
            requested_url="",
            final_url="",
            title="",
            html_len=0,
            signals=["no_url_for_platform"],
        )

    # Новый контекст — чтобы не тащить куки между платформами.
    context = await browser.new_context(
        viewport={"width": 1280, "height": 720},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        locale="en-US",
    )
    page = await context.new_page()
    try:
        final_url, title, html_len, snippet = await _load_page(page, requested_url)
        signals = _detect_signals(snippet)
        return PilotResult(
            platform=platform,
            query_raw=query_raw,
            query_prepared=prepared,
            requested_url=requested_url,
            final_url=final_url,
            title=title,
            html_len=html_len,
            signals=signals,
        )
    finally:
        await context.close()


async def main() -> None:
    queries = [x["value"] for x in suggest_products(None, limit=10)]
    platforms = ["AliExpress", "Pinduoduo"]

    results: list[PilotResult] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            for q in queries:
                for platform in platforms:
                    try:
                        r = await _run_one(
                            browser=browser, platform=platform, query_raw=q
                        )
                        results.append(r)
                        sig = (", ".join(r.signals) if r.signals else "ok")
                        print(
                            f"[{platform}] q={q!r} prepared={r.query_prepared!r} "
                            f"final={r.final_url} title={r.title!r} "
                            f"html={r.html_len} signals={sig}"
                        )
                    except Exception as e:
                        print(f"[{platform}] q={q!r} ERROR: {e!r}")
        finally:
            await browser.close()

    # Краткая сводка:
    failures = [r for r in results if r.signals and "ok" not in r.signals]
    print(f"\nSummary: {len(results)} checks, failures-signals={len(failures)}")


if __name__ == "__main__":
    asyncio.run(main())

