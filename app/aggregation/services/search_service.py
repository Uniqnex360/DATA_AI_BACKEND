import logging
import httpx
from typing import List
from firecrawl import Firecrawl

from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential
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
BLOCKED_PATH_PATTERNS = [
    "/news/", "/blog/", "/category/", "/tag/", "/author/",
    "/contact", "/about", "/faq", "/support", "/help", "/forum",
    "/search", "/cart", "/checkout", "/account", "/login", "/register"
]
from firecrawl import Firecrawl

class SerpApiSearchService:
    """Firecrawl search service"""
    
    def __init__(self, max_results: int = 5):
        self.max_results = max_results
        self.firecrawl = Firecrawl(api_key=settings.FIRECRAWL_API_KEY)
    
    async def get_urls(self, query: str, mpn: str, brand: str, title: Optional[str] = None) -> List[str]:
        search_query = (
            f"{brand} {mpn} {title}".strip()[:120]
            if title and len(title.strip()) > 5
            else query
        )
        search_query = search_query.replace("site:", "").strip()
        
        try:
            results = self.firecrawl.search(query=search_query, limit=self.max_results)
            
            # Handle tuple response
            raw_urls = []
            if isinstance(results, tuple):
                results = results[0] if results else []
            
            for r in results:
                url = r.get("url") if isinstance(r, dict) else r
                if url:
                    raw_urls.append(url)
            
            raw_urls = list(set(self._normalize_url(u) for u in raw_urls if u))
            filtered = [url for url in raw_urls if self._is_valid_product_url(url)]
            ranked = self._rank_urls(filtered, mpn=mpn, brand=brand)
            
            logger.info(f"[Firecrawl] Query='{search_query}' Raw={len(raw_urls)} Filtered={len(filtered)} Ranked={len(ranked)}")
            return ranked[:self.max_results]
        
        except Exception as e:
            logger.error(f"Firecrawl search failed: {e}")
            return []
    
    def _normalize_url(self, url: str) -> str:
        return url.strip()
    
    def _is_valid_product_url(self, url: str) -> bool:
        lower = url.lower()
        path = urlparse(url).path.lower()
        if any(keyword in lower for keyword in BLOCKED_KEYWORDS):
            return False
        if any(pattern in path for pattern in BLOCKED_PATH_PATTERNS):
            return False
        return True
    
    def _rank_urls(self, urls: List[str], mpn: Optional[str] = None, brand: Optional[str] = None) -> List[str]:
        def score(url: str) -> int:
            lower = url.lower()
            path = urlparse(url).path.lower()
            s = 0
            mpn_lower = mpn.lower() if mpn else ""
            
            if mpn_lower and mpn_lower in path:
                s += 300
            elif mpn_lower and mpn_lower in lower:
                s += 100
            
            if path in ['/', ''] or path.rstrip('/') == '':
                s -= 500
            
            if brand:
                brand_base = brand.lower().replace(' ', '').replace('.', '')
                if brand_base in lower:
                    s += 50
            
            if any(p in path for p in ['/product/', '/item/', '/dp/', '/p/']):
                s += 80
            
            if any(domain in lower for domain in PRIORITY_RETAILERS):
                s += 60
            
            return s
        
        scored = [(url, score(url)) for url in urls]
        filtered = [(url, s) for url, s in scored if s > -200]
        filtered.sort(key=lambda x: x[1], reverse=True)
        return [url for url, s in filtered]
    
    async def search(self, query: str, num: int = 5) -> List[dict]:
        try:
            results = self.firecrawl.search(query=query, limit=num)

            normalized = []

            # Firecrawl v2 returns SearchData object with .web attribute
            web_results = getattr(results, "web", None) or []

            for r in web_results:
                url = getattr(r, "url", None)
                title = getattr(r, "title", "")

                if url:
                    normalized.append({
                        "link": url,
                        "title": title
                    })

            return normalized[:num]

        except Exception as e:
            logger.error(f"Firecrawl search failed: {e}")
            return []