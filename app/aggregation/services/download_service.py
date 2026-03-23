# import logging
# import httpx
# from typing import Dict, Optional
# from app.aggregation.interfaces import IDownloadService
# logger = logging.getLogger("download_service")
# import random
# class HttpDownloadService(IDownloadService):

#     def __init__(self, timeout: int = 30,proxy: Optional[str] = None):
#         self.timeout = timeout
#         self.proxy=proxy
#         self.user_agents = [
#             'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
#             'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
#             'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
#             'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
#             'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15'
#         ]

#     async def download(self, url: str) -> Optional[Dict]:
#         try:
#             async with httpx.AsyncClient(
#                 proxies=self.proxy,
#                 verify=False,
#                 timeout=self.timeout,
#                 headers = {
#     'User-Agent': random.choice(self.user_agents),  
#     'Accept-Language': 'en-US,en;q=0.9',
#     'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
#     'Accept-Encoding': 'gzip, deflate, br',
#     'DNT': '1',
#     'Connection': 'keep-alive',
#     'Upgrade-Insecure-Requests': '1',
#     'Sec-Fetch-Dest': 'document',
#     'Sec-Fetch-Mode': 'navigate',
#     'Sec-Fetch-Site': 'none',
#     'Cache-Control': 'max-age=0',
#                 }

#             ) as client:

#                 response = await client.get(
#                     url,
#                     timeout=self.timeout,
#                     follow_redirects=True
#                 )

#                 if response.status_code in [403, 401, 429]:
#                     response = await client.get(url,timeout=self.timeout,follow_redirects=True,headers={**client.headers,"User-Agent": random.choice(self.user_agents)})
#                     if response.status_code in [403, 401, 429]:
#                         return None

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
import logging
import asyncio
from typing import Dict, Optional
from app.aggregation.interfaces import IDownloadService

# NEW IMPORT: Replace httpx with curl_cffi
from curl_cffi import requests 

logger = logging.getLogger("download_service")

class HttpDownloadService(IDownloadService):

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        # We don't need a huge list of user-agents anymore, curl_cffi handles it

    async def download(self, url: str) -> Optional[Dict]:
        try:
            # Use curl_cffi's AsyncSession instead of httpx
            async with requests.AsyncSession(
                impersonate="chrome110", # <--- THE MAGIC WAND: Fakes a real Chrome browser
                timeout=self.timeout,
                verify=False # curl_cffi handles this better than httpx
            ) as client:

                response = await client.get(
                    url,
                    allow_redirects=True
                )

                if response.status_code in [403, 401, 429]:
                    logger.warning(f"⛔ Blocked ({response.status_code}) by {url}. Retrying...")
                    await asyncio.sleep(2)
                    
                    # Try one more time with a slightly different browser fingerprint
                    async with requests.AsyncSession(impersonate="safari15_3", timeout=self.timeout, verify=False) as fallback_client:
                        response = await fallback_client.get(url, allow_redirects=True)
                        if response.status_code in [403, 401, 429]:
                            logger.warning(f"⛔ Still blocked after retry on {url}.")
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

# import logging
# import asyncio
# import random
# from typing import Dict, Optional
# from urllib.parse import urlparse
# from curl_cffi import requests
# from playwright.async_api import async_playwright

# logger = logging.getLogger("download_service")

# # List of browser fingerprints to rotate
# BROWSERS = [
#     "chrome110", "chrome116", "chrome123",
#     "firefox102", "firefox115",
#     "safari15_3", "edge101"
# ]

# # Domains that are known to block curl_cffi and may need Playwright
# STUBBORN_DOMAINS = [
#     "www.anixter.com", "www.hisco.com", "www.mouser.com",
#     "www.homedepot.com", "www.lowes.com"
# ]

# class HttpDownloadService:
#     def __init__(self, timeout: int = 30):
#         self.timeout = timeout
#         self._sessions = {}  # optional: reuse sessions per domain (not implemented here for simplicity)

#     async def download(self, url: str) -> Optional[Dict]:
#         # Determine if this domain is known to be stubborn
#         domain = urlparse(url).netloc
#         use_playwright = domain in STUBBORN_DOMAINS

#         # Try curl_cffi first (unless it's a known stubborn domain, then go straight to Playwright)
#         if not use_playwright:
#             for attempt in range(3):
#                 try:
#                     # Add random delay before request to mimic human behavior
#                     await asyncio.sleep(random.uniform(1.0, 3.0))
#                     impersonate = random.choice(BROWSERS)
#                     async with requests.AsyncSession(
#                         impersonate=impersonate,
#                         timeout=self.timeout,
#                         verify=False
#                     ) as session:
#                         response = await session.get(url, allow_redirects=True)

#                         if response.status_code == 200:
#                             # Success with curl_cffi
#                             is_pdf = "pdf" in response.headers.get("Content-Type", "").lower()
#                             return {
#                                 "source_url": url,
#                                 "raw_bytes": response.content,
#                                 "type": "pdf" if is_pdf else "html",
#                             }
#                         elif response.status_code in [403, 429]:
#                             logger.warning(f"Blocked ({response.status_code}) attempt {attempt+1} for {url}")
#                             await asyncio.sleep(2 ** attempt)  # exponential backoff
#                             continue
#                         else:
#                             # Other status code -> give up
#                             logger.warning(f"HTTP {response.status_code} for {url}")
#                             break
#                 except Exception as e:
#                     logger.error(f"curl_cffi attempt {attempt+1} failed for {url}: {e}")
#                     await asyncio.sleep(2 ** attempt)

#         # If we reach here, curl_cffi failed or domain is stubborn → use Playwright
#         logger.info(f"Falling back to Playwright for {url}")
#         try:
#             content = await self._download_with_playwright(url)
#             if content:
#                 # Playwright returns HTML content as string; we treat it as HTML
#                 return {
#                     "source_url": url,
#                     "raw_bytes": content.encode('utf-8'),
#                     "type": "html",
#                 }
#         except Exception as e:
#             logger.error(f"Playwright download failed for {url}: {e}")

#         return None

#     async def _download_with_playwright(self, url: str) -> Optional[str]:
#         """Fetch page content using Playwright (headless browser)."""
#         try:
#             async with async_playwright() as p:
#                 browser = await p.chromium.launch(headless=True)
#                 page = await browser.new_page()
#                 await page.goto(url, timeout=self.timeout * 1000)
#                 content = await page.content()
#                 await browser.close()
#                 return content
#         except Exception as e:
#             logger.error(f"Playwright error for {url}: {e}")
#             return None