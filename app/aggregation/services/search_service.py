
import logging
import httpx
from typing import List
from app.core.config import settings
from app.aggregation.interfaces import ISearchService

logger = logging.getLogger("search_service")


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
                urls = [
                    r["link"]
                    for r in data.get("organic_results", [])
                    if r.get("link")
                ]
                return urls[:self.max_results]
        
        except Exception as e:
            logger.warning(f"SerpAPI failed: {e}")
            return []