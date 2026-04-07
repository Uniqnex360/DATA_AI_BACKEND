import logging
import asyncio
from typing import Dict, Optional
from app.aggregation.interfaces import IDownloadService
from curl_cffi import requests 
logger = logging.getLogger("download_service")
class HttpDownloadService(IDownloadService):
    def __init__(self, timeout: int = 30):
        self.timeout = timeout
    async def download(self, url: str) -> Optional[Dict]:
        try:
            async with requests.AsyncSession(
                impersonate="chrome110", 
                timeout=self.timeout,
                verify=False 
            ) as client:
                response = await client.get(
                    url,
                    allow_redirects=True
                )
                if response.status_code in [403, 401, 429]:
                    logger.warning(f"Blocked ({response.status_code}) by {url}. Retrying...")
                    await asyncio.sleep(2)
                    async with requests.AsyncSession(impersonate="safari15_3", timeout=self.timeout, verify=False) as fallback_client:
                        response = await fallback_client.get(url, allow_redirects=True)
                        if response.status_code in [403, 401, 429]:
                            logger.warning(f" Still blocked after retry on {url}.")
                            return None
                if response.status_code != 200:
                    return None
                is_pdf = "pdf" in response.headers.get("Content-Type", "").lower()
                return {
                    "source_url": url,
                    "raw_bytes": response.content,
                    "type": "pdf" if is_pdf else "html",
                }
        except Exception as e:
            logger.error(f"Download crashed for {url}: {type(e).__name__} - {e}")
            return None
from playwright.async_api import async_playwright
async def download_with_playwright(self, url: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, timeout=self.timeout * 1000)
        content = await page.content()
        await browser.close()
        return content
