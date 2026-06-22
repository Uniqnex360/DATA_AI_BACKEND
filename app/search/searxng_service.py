import logging
from typing import List, Optional
import aiohttp
from app.aggregation.interfaces import ISearchService
import httpx
logger = logging.getLogger("searxng_search")
class SearXNGSearchService(ISearchService):
    def __init__(
        self,
        base_url: str = "http://localhost:8888",
        max_results: int = 5,
        timeout: int = 15,
        engines: Optional[List[str]] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.max_results = max_results
        self.timeout = timeout
        self.engines = engines or ["google", "bing", "duckduckgo", "brave"]
    async def search_images(self, query: str) -> List[dict]:
        params = {
            "q": query,
            "format": "json",
            "categories": "images",
            "pageno": 1,
        }
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/search", params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            
            return data.get("results", [])
    async def get_urls(
        self, query: str, mpn: str, brand: str
    ) -> List[str]:
        search_query = self._build_query(mpn, brand)
        try:
            results = await self._search(search_query)
            urls = [r["url"] for r in results[:self.max_results]]
            logger.info(f"SearXNG found {len(urls)} URLs for '{search_query}'")
            return urls
        except Exception as e:
            logger.exception(f"SearXNG search failed for '{search_query}': {e}")
            return []
    def _build_query(self, mpn: str, brand: str, sku: str = None) -> str:
        parts = []
        if mpn:
           parts.append(mpn) 
        if brand:
            parts.append(brand) 
        
        
        if sku and sku != mpn and len(sku) > 3:
            parts.append(sku)
        # if title:
        #     title_lower = title.lower()
        #     mpn_lower = (mpn or "").lower()
        #     brand_lower = (brand or "").lower()
        #     sku_lower = (sku or "").lower()
        #     # remainder = title_lower
        #     remainder = remainder.replace(mpn_lower, "")
        #     remainder = remainder.replace(brand_lower, "")
        #     remainder = remainder.replace(sku_lower, "").strip()
        #     STOP_WORDS = {
        #         'with', 'from', 'that', 'this', 'tool', 'unit', 'bare', 'only',
        #         'and', 'for', 'the', 'kit', 'pack', 'set', 'free', 'running',
        #         'wire', 'feed', 'strip', 'white', '1k', '2k', 'bulk'
        #     }
        #     words = [
        #         w for w in remainder.split()
        #         if len(w) > 2
        #         and w not in STOP_WORDS
        #         and not w.isdigit()
        #     ]
            
        #     max_words = 5 if len(mpn) < 6 else 3
        #     if words:
        #         parts.append(" ".join(words[:max_words]))
        parts.append("specifications")
        return " ".join(parts)

    async def _search(self, query: str, engines: Optional[str] = None) -> List[dict]:
        params = {
            "q": query,
            "format": "json",
            "engines": ",".join(self.engines),
            "categories": "general",
            "pageno": 1,
        }
        if engines:
            params["engines"] = engines
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.base_url}/search",
                params=params,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise Exception(f"SearXNG returned {resp.status}: {text[:200]}")
                data = await resp.json()
                results = data.get("results", [])
                results.sort(key=lambda r: r.get("score", 0), reverse=True)
                return data.get("results", [])