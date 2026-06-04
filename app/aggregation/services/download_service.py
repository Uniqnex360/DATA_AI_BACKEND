import logging
import asyncio
import gc  
from typing import Dict, Optional
from app.aggregation.interfaces import IDownloadService
from curl_cffi import requests 
from playwright.async_api import async_playwright

logger = logging.getLogger("download_service")



playwright_lock = asyncio.Semaphore(1)

class HttpDownloadService(IDownloadService):
    def __init__(self, timeout: int = 30, use_playwright_fallback: bool = True, proxy: Optional[str] = None):
        self.timeout = timeout
        self.use_playwright_fallback = use_playwright_fallback
        self.proxy = proxy

    async def download(self, url: str) -> Optional[Dict]:
        result = await self._download_curl(url)
        if result is None and self.use_playwright_fallback:
            logger.info(f"curl_cffi failed for {url}, trying Playwright...")
            result = await self._download_playwright(url)
        return result

    async def _download_curl(self, url: str) -> Optional[Dict]:
        try:
            proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
            async with requests.AsyncSession(
                impersonate="chrome120",
                timeout=self.timeout,
                verify=False,
                proxies=proxies
            ) as client:
                response = await client.get(url, allow_redirects=True)
                if response.status_code in [403, 401, 429]:
                    logger.warning(f"Blocked ({response.status_code}) by {url}. Retrying with Safari...")
                    await asyncio.sleep(2)
                    async with requests.AsyncSession(
                        impersonate="safari17_0",
                        timeout=self.timeout,
                        verify=False,
                        proxies=proxies
                    ) as fallback:
                        response = await fallback.get(url, allow_redirects=True)
                
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
        finally:
            
            gc.collect()

    async def _download_playwright(self, url: str) -> Optional[Dict]:
        
        async with playwright_lock:
            try:
                async with async_playwright() as p:
                    launch_args = {
                        "headless": True,
                        "args": [
                            "--no-sandbox",
                            "--disable-setuid-sandbox",
                            "--disable-dev-shm-usage", 
                            "--disable-gpu",            
                            "--disable-blink-features=AutomationControlled"
                        ]
                    }
                    if self.proxy:
                        launch_args["proxy"] = {"server": self.proxy}

                    browser = await p.chromium.launch(**launch_args)
                    context = await browser.new_context(
                        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "Chrome/120.0.0.0 Safari/537.36"),
                        viewport={"width": 1920, "height": 1080}
                    )
                    page = await context.new_page()

                    
                    await page.goto(url, timeout=self.timeout * 1000, wait_until="networkidle")
                    await page.wait_for_timeout(1500)

                    
                    await page.evaluate("""
                        async () => {
                            await new Promise(resolve => {
                                let totalHeight = 0;
                                let distance = 500;
                                let timer = setInterval(() => {
                                    window.scrollBy(0, distance);
                                    totalHeight += distance;
                                    if(totalHeight >= document.body.scrollHeight){
                                        clearInterval(timer);
                                        resolve();
                                    }
                                }, 300);
                            });
                        }
                    """)
                    await page.wait_for_timeout(1500)

                    
                    expand_selectors = [
                        "text=Specifications", "text=Specification", "text=Product Details",
                        "text=View Specifications", "text=Show More", "text=View More",
                        "button:has-text('Specifications')", "a:has-text('Specifications')"
                    ]
                    for selector in expand_selectors:
                        try:
                            elements = await page.query_selector_all(selector)
                            for el in elements:
                                try:
                                    await el.click()
                                    await page.wait_for_timeout(800)
                                except: pass
                        except: pass

                    
                    try:
                        buttons = await page.query_selector_all("button")
                        for btn in buttons:
                            try:
                                text = await btn.inner_text()
                                if any(keyword in text.lower() for keyword in ["spec", "expand", "more", "details"]):
                                    await btn.click()
                                    await page.wait_for_timeout(500)
                            except: pass
                    except: pass

                    await page.wait_for_timeout(2000)
                    content = await page.content()
                    

                    await browser.close()

                    if content and len(content) > 20000:
                        logger.info(f"✅ Playwright FULL HTML fetched: {len(content)} bytes from {url}")
                        return {
                            "source_url": url,
                            "raw_bytes": content.encode("utf-8"),
                            "type": "html",
                        }
                    logger.warning(f"⚠ Playwright returned small HTML ({len(content)}) for {url}")
                    return None

            except Exception as e:
                logger.error(f"Playwright download failed for {url}: {e}")
                return None
            finally:
                
                gc.collect()