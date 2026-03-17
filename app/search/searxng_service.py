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
            resp = await client.get(f"{self.base_url}/search", params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            # SearXNG returns images with 'img_src' and 'thumbnail_src'
            return data.get("results", [])
    async def get_urls(
        self, query: str, mpn: str, brand: str, title: str
    ) -> List[str]:
        search_query = self._build_query(mpn, brand, title)

        try:
            results = await self._search(search_query)
            urls = [r["url"] for r in results[:self.max_results]]
            logger.info(f"SearXNG found {len(urls)} URLs for '{search_query}'")
            return urls

        except Exception as e:
            logger.exception(f"SearXNG search failed for '{search_query}': {e}")
            return []

    def _build_query(self, mpn: str, brand: str, title: str) -> str:
        parts = []
        if brand:
            parts.append(brand)
        if mpn:
            parts.append(mpn)
        if title and title.lower() != mpn.lower():
            parts.append(title)
        parts.append("product specifications")
        return " ".join(parts)

    async def _search(self, query: str) -> List[dict]:
        params = {
            "q": query,
            "format": "json",
            "engines": ",".join(self.engines),
            "categories": "general",
        }

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
                return data.get("results", [])