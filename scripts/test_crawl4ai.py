# scripts/test_crawl4ai.py
import asyncio
import logging
import sys
from typing import Optional

from crawl4ai import (
    AsyncWebCrawler,
    BrowserConfig,
    CrawlerRunConfig,
    CacheMode,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("crawl4ai_test")


async def fetch_html_with_crawl4ai(url: str) -> Optional[str]:
    browser_config = BrowserConfig(
        headless=True,
        browser_type="chromium",
    )

    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        wait_until="domcontentloaded",   # ✅ lifecycle event goes here
        page_timeout=45000,              # ms
        delay_before_return_html=2.0,    # let JS hydrate a bit
        # wait_for="css:.pdp-main",      # optional: real CSS selector, prefix with css:
        scan_full_page=True,             # scroll to trigger lazy loading
        magic=True,                      # basic anti-bot/cookie-banner handling
    )

    logger.info(f"Starting Crawl4AI fetch for: {url}")

    try:
        async with AsyncWebCrawler(config=browser_config) as crawler:
            result = await crawler.arun(url=url, config=run_config)

        if result is None:
            logger.warning("Crawl4AI returned no result object")
            return None

        logger.info(
            "success=%s status_code=%s error=%s",
            getattr(result, "success", None),
            getattr(result, "status_code", None),
            getattr(result, "error_message", None),
        )

        return getattr(result, "html", None) or getattr(result, "cleaned_html", None)

    except Exception as e:
        logger.exception(f"Crawl4AI fetch failed for {url}: {e}")
        return None


async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else \
        "https://www.aeg.ie/laundry/washing-machines/washing-machine/L6FBK141B/"

    logger.info(f"Testing Crawl4AI on: {url}")
    html = await fetch_html_with_crawl4ai(url)

    if html:
        logger.info(f"Fetched {len(html)} characters of HTML")
        with open("crawl4ai_test.html", "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Wrote HTML to crawl4ai_test.html")
    else:
        logger.warning("No HTML returned from Crawl4AI")


if __name__ == "__main__":
    asyncio.run(main())