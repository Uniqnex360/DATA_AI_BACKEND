import logging
import asyncio
from typing import Dict, Optional
from app.aggregation.interfaces import IDownloadService
from curl_cffi import requests 
logger = logging.getLogger("download_service")
from playwright.async_api import async_playwright

# class HttpDownloadService(IDownloadService):
#     def __init__(self, timeout: int = 30):
#         self.timeout = timeout
#     async def download(self, url: str) -> Optional[Dict]:
#         try:
#             async with requests.AsyncSession(
#                 impersonate="chrome110", 
#                 timeout=self.timeout,
#                 verify=False 
#             ) as client:
#                 response = await client.get(
#                     url,
#                     allow_redirects=True
#                 )
#                 if response.status_code in [403, 401, 429]:
#                     logger.warning(f"Blocked ({response.status_code}) by {url}. Retrying...")
#                     await asyncio.sleep(2)
#                     async with requests.AsyncSession(impersonate="safari15_3", timeout=self.timeout, verify=False) as fallback_client:
#                         response = await fallback_client.get(url, allow_redirects=True)
#                         if response.status_code in [403, 401, 429]:
#                             logger.warning(f" Still blocked after retry on {url}.")
#                             return None
#                 if response.status_code != 200:
#                     return None
#                 is_pdf = "pdf" in response.headers.get("Content-Type", "").lower()
#                 return {
#                     "source_url": url,
#                     "raw_bytes": response.content,
#                     "type": "pdf" if is_pdf else "html",
#                 }
#         except Exception as e:
#             logger.error(f"Download crashed for {url}: {type(e).__name__} - {e}")
#             return None
# from playwright.async_api import async_playwright
# async def download_with_playwright(self, url: str):
#     async with async_playwright() as p:
#         browser = await p.chromium.launch(headless=True)
#         page = await browser.new_page()
#         await page.goto(url, timeout=self.timeout * 1000)
#         content = await page.content()
#         await browser.close()
#         return content
class HttpDownloadService(IDownloadService):
    def __init__(self, timeout: int = 30, use_playwright_fallback: bool = True):
        self.timeout = timeout
        self.use_playwright_fallback = use_playwright_fallback
    
    async def download(self, url: str) -> Optional[Dict]:
        result = await self._download_curl(url)
        if result is None and self.use_playwright_fallback:
            logger.info(f"curl_cffi failed for {url}, trying Playwright...")
            result = await self._download_playwright(url)
        return result
    
    async def _download_curl(self, url: str) -> Optional[Dict]:
        try:
            # Try Chrome impersonation
            async with requests.AsyncSession(
                impersonate="chrome120",
                timeout=self.timeout,
                verify=False
            ) as client:
                response = await client.get(url, allow_redirects=True)
                
                if response.status_code in [403, 401, 429]:
                    logger.warning(f"Blocked ({response.status_code}) by {url}. Retrying with Safari...")
                    await asyncio.sleep(2)
                    # Retry with Safari impersonation
                    async with requests.AsyncSession(
                        impersonate="safari17_0",
                        timeout=self.timeout,
                        verify=False
                    ) as fallback:
                        response = await fallback.get(url, allow_redirects=True)
                        if response.status_code in [403, 401, 429]:
                            logger.warning(f"Still blocked after retry on {url}.")
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
    
    async def _download_playwright(self, url: str) -> Optional[Dict]:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-setuid-sandbox']
                )
                page = await browser.new_page()
                await page.goto(url, timeout=self.timeout * 1000, wait_until="domcontentloaded")
                content = await page.content()
                await browser.close()
                
                if content and len(content) > 500:
                    return {
                        "source_url": url,
                        "raw_bytes": content.encode('utf-8'),
                        "type": "html",
                    }
                return None
        except Exception as e:
            logger.error(f"Playwright download failed for {url}: {e}")
            return None