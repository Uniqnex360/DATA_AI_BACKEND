import logging
import asyncio
import gc  
from typing import Dict, Optional
from app.aggregation.interfaces import IDownloadService
from curl_cffi import requests 
from playwright.async_api import async_playwright

logger = logging.getLogger("download_service")



playwright_lock = asyncio.Semaphore(3)

class HttpDownloadService(IDownloadService):
    _playwright = None
    _browser = None
    _browser_lock = asyncio.Lock()
    def __init__(self, timeout: int = 30, use_playwright_fallback: bool = True, proxy: Optional[str] = None):
        self.timeout = timeout
        self.use_playwright_fallback = use_playwright_fallback
        self.proxy = proxy
        self._cache = {}
    async def _get_browser(self):
        async with HttpDownloadService._browser_lock:
            if HttpDownloadService._browser is None:
                HttpDownloadService._playwright = await async_playwright().start()

                launch_args = {
                    "headless": True,
                    "args": [
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-blink-features=AutomationControlled",
                    ],
                }

                if self.proxy:
                    launch_args["proxy"] = {"server": self.proxy}

                HttpDownloadService._browser = (
                    await HttpDownloadService._playwright.chromium.launch(**launch_args)
                )

        return HttpDownloadService._browser
    async def close_browser(self):
        if HttpDownloadService._browser:
            await HttpDownloadService._browser.close()
            HttpDownloadService._browser = None

        if HttpDownloadService._playwright:
            await HttpDownloadService._playwright.stop()
            HttpDownloadService._playwright = None
    async def download(self, url: str) -> Optional[Dict]:
        if url in self._cache:
            return self._cache[url]
        result = await self._download_curl(url)
                
        needs_playwright = False
        if result and result.get('type') == 'html':
            html_text = result.get('raw_bytes', b'').decode('utf-8', errors='ignore')
            
            nav_count = html_text.count('mobile-nav') + html_text.count('data-testid="mobile-nav')
            import re
            has_spec_values = bool(
                re.search(r'(?:height|width|length|weight|depth)\s*[":]\s*\d+\.?\d*\s*(?:in|cm|mm|lb|kg|oz|ft)', html_text, re.IGNORECASE)
            )
            is_too_small = len(html_text) < 5000
            is_bot_blocked = (
                "just a moment" in html_text.lower() or
                "verify you are human" in html_text.lower() or
                "enable javascript and cookies" in html_text.lower() or
                ("cloudflare" in html_text.lower() and "ray id" in html_text.lower()) or
                "access denied" in html_text.lower()
            )
            logger.info(
            f"Shell detection: nav_count={nav_count}, has_spec_values={has_spec_values}, "
            f"size={len(html_text)}b, too_small={is_too_small}, bot_blocked={is_bot_blocked}"
            )

            if (nav_count > 15 and not has_spec_values) or is_too_small or is_bot_blocked:
                needs_playwright = True
                logger.info(f"TRIGGERING Playwright fallback for {url} "
                            f"(big_shell={nav_count > 15 and not has_spec_values}, "
                            f"too_small={is_too_small}, bot_blocked={is_bot_blocked})")
            else:
                logger.info(f"NOT triggering Playwright for {url}")
        
        if (result is None or needs_playwright) and self.use_playwright_fallback:

            logger.info(f"curl_cffi {'failed' if not result else 'got shell HTML'} for {url}, trying Playwright...")
            pw_result = await self._download_playwright(url)
            if pw_result:
                result = pw_result
        if result:
            self._cache[url] = result
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
                content_type = response.headers.get("Content-Type", "").lower()
                is_pdf = "pdf" in content_type or url.lower().split('?')[0].endswith('.pdf')
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
        page = None
        context = None
        is_pdf_url = url.lower().split('?')[0].endswith('.pdf')
        async with playwright_lock:
            try:
                # launch_args = {
                #     "headless": True,
                #     "args": [
                #         "--no-sandbox",
                #         "--disable-setuid-sandbox",
                #         "--disable-dev-shm-usage", 
                #         "--disable-gpu",            
                #         "--disable-blink-features=AutomationControlled"
                #     ]
                # }
                # if self.proxy:
                #     launch_args["proxy"] = {"server": self.proxy}

                # browser = await p.chromium.launch(**launch_args)
                browser = await self._get_browser()
                context = await browser.new_context(
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "Chrome/120.0.0.0 Safari/537.36"),
                    viewport={"width": 1920, "height": 1080},
                    ignore_https_errors=True
                )
                page = await context.new_page()

                
                response=await page.goto(url, timeout=self.timeout * 1000, wait_until="networkidle")
                await page.wait_for_timeout(1500)

                content_type = response.headers.get(
                    "Content-Type", "").lower() if response else ""
                is_pdf = is_pdf_url or "pdf" in content_type
                if is_pdf:
                    logger.info(
                        f"Playwright detected PDF at {url}. Fetching raw bytes using authenticated context...")
                    try:
                        pdf_response = await context.request.get(url)
                        if pdf_response.ok:
                            raw_bytes = await pdf_response.body()
                            
                            # await browser.close()
                            logger.info(
                                f"✓ Playwright successfully downloaded PDF: {len(raw_bytes)} bytes from {url}")
                            return {
                                "source_url": url,
                                "raw_bytes": raw_bytes,
                                "type": "pdf",
                            }
                        else:
                            logger.warning(
                                f"Failed to fetch PDF bytes for {url} (Status: {pdf_response.status})")
                            
                            # await browser.close()
                            return None
                    except Exception as pdf_e:
                        logger.error(
                            f"Error fetching PDF bytes for {url}: {pdf_e}")
                        
                        # await browser.close()
                        return None
                await page.evaluate("""
                    async () => {
                        await new Promise(resolve => {
                            let totalHeight = 0;
                            let distance = 500;
                            let timer = setInterval(() => {
                                window.scrollBy(0, distance);
                                totalHeight += distance;
                                if(totalHeight >= document.body.scrollHeight || totalHeight > 10000){
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
                
               
                # await browser.close()

                if content and len(content) > 5000:
                    logger.info(f"Playwright FULL HTML fetched: {len(content)} bytes from {url}")
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
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass

                if context:
                    try:
                        await context.close()
                    except Exception:
                        pass

                gc.collect()
download_service = HttpDownloadService(timeout=20)
