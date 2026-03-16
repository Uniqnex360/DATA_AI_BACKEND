import logging
import httpx
from typing import List
from app.core.config import settings
from app.aggregation.interfaces import ISearchService
from typing import Optional
from urllib.parse import urlparse
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

    async def get_urls(self, query: str, mpn: str, brand: str) -> List[str]:
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
                ranked = self._rank_urls(filtered, mpn=mpn, brand=brand)
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

    def _rank_urls(self, urls: List[str], mpn: Optional[str] = None, brand: Optional[str] = None) -> List[str]:
        def score(url: str) -> int:
            lower = url.lower()
            path = urlparse(url).path.lower()
            s = 0

            # --- EXACT MPN MATCH (Highest Priority) ---
            # Check if MPN appears as a whole word/segment in the URL path
            if mpn and (f"/{mpn.lower()}" in path or f"{mpn.lower()}/" in path or path.split('/')[-1] == mpn.lower()):
                s += 500  # Much higher score
            # Also check in the full URL if not in path
            elif mpn and mpn.lower() in lower:
                s += 200

            # --- PENALIZE OTHER PRODUCT CODES ---
            # Simple check for other common numeric codes (you might need a list)
            if 'hs-620' in lower or 'hs620' in lower:
                s -= 500  # Severe penalty for known wrong product

            # --- Manufacturer Domain ---
            if brand:
                brand_base = brand.lower().replace(' ', '').replace('.', '')
                if brand_base in lower:
                    s += 100

            # --- Product Page Indicators ---
            if any(p in lower for p in ['/product/', '/item/', '/dp/', '/p-', '/products/', '/catalog/']):
                s += 80

            # --- Valuable PDFs ---
            if lower.endswith('.pdf'):
                if 'datasheet' in lower or 'spec' in lower:
                    s += 150
                else:
                    s += 50

            # --- Retailers (lower priority than exact MPN) ---
            if any(domain in lower for domain in PRIORITY_RETAILERS):
                s += 60

            # --- Penalize generic pages ---
            if any(b in lower for b in ['/contact', '/about', '/news', '/blog', '/product-information']):
                s -= 100

            return s

        return sorted(urls, key=score, reverse=True)
