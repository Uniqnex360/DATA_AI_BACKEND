# app/services/crawl4ai_service.py
import logging
from typing import Optional

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode

logger = logging.getLogger("crawl4ai_service")


async def render_with_crawl4ai(url: str) -> Optional[str]:
    """Return rendered HTML for a URL using Crawl4AI, or None on failure."""
    browser_config = BrowserConfig(
        headless=True,
        browser_type="chromium",
    )

    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        wait_until="domcontentloaded",   # lifecycle event
        page_timeout=45000,              # ms
        delay_before_return_html=2.0,    # small extra wait for JS
        scan_full_page=True,
        magic=True,
    )

    logger.info(f"[Crawl4AI] Fetching: {url}")
    try:
        async with AsyncWebCrawler(config=browser_config) as crawler:
            result = await crawler.arun(url=url, config=run_config)

        if not result:
            logger.warning("[Crawl4AI] No result object returned")
            return None

        logger.info(
            "[Crawl4AI] success=%s status_code=%s error=%s",
            getattr(result, "success", None),
            getattr(result, "status_code", None),
            getattr(result, "error_message", None),
        )

        html = getattr(result, "html", None) or getattr(result, "cleaned_html", None)
        if html and len(html) > 0:
            return html

        return None

    except Exception as e:
        logger.warning(f"[Crawl4AI] Failed for {url}: {e!r}")
        return None