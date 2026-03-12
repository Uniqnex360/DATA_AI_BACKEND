import logging
import httpx
from typing import List
from urllib.parse import urlparse
from app.core.config import settings
from app.aggregation.interfaces import ISearchService

logger = logging.getLogger("search_service")



PRIORITY_RETAILERS = [
    "amazon.com",
    "walmart.com",
    "bestbuy.com",
    "target.com",
    "bhphotovideo.com",
]


BLOCKED_KEYWORDS = [
    "community",
    "forum",
    "reddit",
    "support",
    "manual",
    "help",
    "faq",
    "question",
]

class SerpApiSearchService(ISearchService):
    
    def __init__(self, max_results: int = 5):
        self.max_results = max_results

    async def get_urls(self, query: str) -> List[str]:
        if not settings.serpapi_key:
            logger.error("SerpAPI key missing")
            return []

        try:
            async with httpx.AsyncClient(verify=False) as client:
                response = await client.get(
                    "https://serpapi.com/search",
                    params={
                        "engine": "google",
                        "q": query,
                        "api_key": settings.serpapi_key,
                        "num": 10,
                    },
                    timeout=20,
                )

                data = response.json()

                raw_urls = [
                    r["link"]
                    for r in data.get("organic_results", [])
                    if r.get("link")
                ]

                logger.info(f" SERP returned {len(raw_urls)} raw URLs")

                
                filtered = [
                    url for url in raw_urls
                    if self._is_valid_product_url(url)
                ]

                logger.info(f"{len(filtered)} URLs after filtering")

                
                ranked = self._rank_urls(filtered)

                return ranked[:self.max_results]

        except Exception as e:
            logger.warning(f"SERP failed: {e}")
            return []

    def _is_valid_product_url(self, url: str) -> bool:
        lower = url.lower()

        
        if any(keyword in lower for keyword in BLOCKED_KEYWORDS):
            return False

        
        if "/product" in lower or "/dp/" in lower:
            return True

        
        if any(domain in lower for domain in PRIORITY_RETAILERS):
            return True

        
        return True

    def _rank_urls(self, urls: List[str]) -> List[str]:
        

        def score(url: str) -> int:
            lower = url.lower()

            
            if any(domain in lower for domain in PRIORITY_RETAILERS):
                return 100

            
            if "/product" in lower or "/dp/" in lower:
                return 80

            
            return 10

        return sorted(urls, key=score, reverse=True)