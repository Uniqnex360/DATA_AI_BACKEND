import logging
import httpx
from typing import List

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

class SerpApiSearchService(ISearchService):

    def __init__(self, max_results: int = 5):
        self.max_results = max_results

    async def get_urls(
        self,
        query: str,
        mpn: str,
        brand: str,
        title: Optional[str] = None
    ) -> List[str]:

        if not settings.serpapi_key:
            logger.error("SerpAPI key missing")
            return []

        search_query = (
            f"{brand} {mpn} {title}".strip()[:120]
            if title and len(title.strip()) > 5
            else query
        )

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(20.0)
            ) as client:

                async for attempt in AsyncRetrying(
                    stop=stop_after_attempt(3),
                    wait=wait_exponential(multiplier=1, min=1, max=8),
                ):
                    with attempt:
                        response = await client.get(
                            "https://serpapi.com/search",
                            params={
                                "engine": "google",
                                "q": search_query,
                                "api_key": settings.serpapi_key,
                                "num": 15,
                            },
                        )

                if response.status_code != 200:
                    logger.error(
                        f"SerpApi HTTP {response.status_code}: "
                        f"{response.text[:300]}"
                    )
                    return []

                data = response.json()

                if "error" in data:
                    logger.error(f"SerpApi error: {data['error']}")
                    return []

                raw_urls = [
                    r["link"]
                    for r in data.get("organic_results", [])
                    if r.get("link")
                ]

                raw_urls = list(set(self._normalize_url(u) for u in raw_urls))

                filtered = [
                    url for url in raw_urls
                    if self._is_valid_product_url(url)
                ]

                ranked = self._rank_urls(filtered, mpn=mpn, brand=brand)

                logger.info(
                    f"[SerpApi] Query='{search_query}' "
                    f"Raw={len(raw_urls)} "
                    f"Filtered={len(filtered)} "
                    f"Ranked={len(ranked)}"
                )

                return ranked[:self.max_results]

        except Exception:
            logger.exception(f"SerpApi search failed: {search_query}")
            return []

    def _is_valid_product_url(self, url: str) -> bool:
        lower = url.lower()
        path = urlparse(url).path.lower()
        if any(keyword in lower for keyword in BLOCKED_KEYWORDS):
            return False
        if any(keyword in lower for keyword in BLOCKED_PATH_PATTERNS):
            return False
        if any(keyword in lower for keyword in BLOCKED_KEYWORDS):
            logger.debug(f"Blocked {url} – contains blocked keyword")
            return False
        if any(pattern in path for pattern in BLOCKED_PATH_PATTERNS):
            logger.debug(f"Blocked {url} – path contains blocked pattern")
            return False
        if "/product" in lower or "/dp/" in lower:
            return True
        if any(domain in lower for domain in PRIORITY_RETAILERS):
            return True
        return True
    # def _rank_urls(self, urls: List[str], mpn: Optional[str] = None, brand: Optional[str] = None) -> List[str]:
    #     def score(url: str) -> int:
    #         lower = url.lower()
    #         path = urlparse(url).path.lower()
    #         s = 0
    #         if mpn and (f"/{mpn.lower()}" in path or f"{mpn.lower()}/" in path or path.split('/')[-1] == mpn.lower()):
    #             s += 500
    #         elif mpn and mpn.lower() in lower:
    #             s += 200
    #         if 'hs-620' in lower or 'hs620' in lower:
    #             s -= 500
    #         if brand:
    #             brand_base = brand.lower().replace(' ', '').replace('.', '')
    #             brand_domain_guess = brand.lower().replace(' ', '').replace('.', '') + '.com'
    #             if brand_domain_guess in lower:
    #                 s+=200
    #             if brand_base in lower:
    #                 s += 100
    #         if any(p in lower for p in ['/product/', '/item/', '/dp/', '/p-', '/products/', '/catalog/']):
    #             s += 80
    #         if lower.endswith('.pdf'):
    #             if 'datasheet' in lower or 'spec' in lower:
    #                 s += 150
    #             else:
    #                 s += 50
    #         if lower.endswith('.pdf') and mpn and mpn.lower() in path:
    #             s+=200
    #         if any(domain in lower for domain in PRIORITY_RETAILERS):
    #             s += 60
    #         if any(b in lower for b in ['/contact', '/about', '/news', '/blog', '/product-information']):
    #             s -= 100
    #         return s
    #     return sorted(urls, key=score, reverse=True)

    def _rank_urls(self, urls: List[str], mpn: Optional[str] = None, brand: Optional[str] = None) -> List[str]:
        def score(url: str) -> int:
            lower = url.lower()
            path = urlparse(url).path.lower()
            s = 0
            mpn_lower = mpn.lower() if mpn else ""
            if mpn_lower:
                path_parts = [p for p in path.split('/') if p]
                if any(mpn_lower == part or mpn_lower == part.replace('-', '') for part in path_parts):
                    s += 500
                elif mpn_lower in path:
                    s += 300
                elif mpn_lower in lower:
                    s += 100
            if mpn_lower:
                path_parts = [p for p in path.split('/') if p]
                for part in path_parts:
                    if (len(part) > 3
                        and part != mpn_lower
                        and part.replace('-', '') != mpn_lower.replace('-', '')
                        and any(c.isdigit() for c in part)
                            and any(c.isalpha() for c in part)):
                        # Looks like another product code
                        s -= 200
            if path in ['/', ''] or path.rstrip('/') == '':
                s -= 500
            generic_patterns = [
                '/contact', '/about', '/news', '/blog',
                '/product-information', '/corporate',
                '/careers', '/privacy', '/terms',
                '/catalog', '/catalogues', '/mpt-catalog'
            ]
            if any(pattern in path for pattern in generic_patterns):
                s -= 300
            if mpn_lower and mpn_lower not in path:
                if any(p in path for p in ['/products/', '/collections/', '/categories/', '/c/']):
                    s -= 200
            if brand:
                brand_base = brand.lower().replace(' ', '').replace('.', '')
                brand_domain = brand_base + '.com'
                if brand_domain in lower:
                    s += 150
                elif brand_base in lower:
                    s += 50
            product_patterns = ['/product/', '/item/',
                                '/items/', '/dp/', '/p-', '/p/']
            if any(p in path for p in product_patterns):
                s += 80
            if lower.endswith('.pdf'):
                if mpn_lower and mpn_lower in path:
                    s += 250
                elif 'datasheet' in lower or 'spec' in lower:
                    s += 100
                else:
                    s -= 50
            if any(domain in lower for domain in PRIORITY_RETAILERS):
                s += 60
            return s
        scored = [(url, score(url)) for url in urls]
        filtered = [(url, s) for url, s in scored if s > -200]
        filtered.sort(key=lambda x: x[1], reverse=True)
        return [url for url, s in filtered]
    async def search(self, query: str, num: int = 5) -> List[dict]:
        """
        Lightweight raw search for discovery purposes.
        Returns raw organic results.
        """
        if not settings.serpapi_key:
            logger.error("SerpAPI key missing")
            return []

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
                response = await client.get(
                    "https://serpapi.com/search",
                    params={
                        "engine": "google",
                        "q": query,
                        "api_key": settings.serpapi_key,
                        "num": num,
                    },
                )

            if response.status_code != 200:
                logger.error(f"SerpApi error {response.status_code}")
                return []

            data = response.json()

            if "error" in data:
                logger.error(f"SerpApi error: {data['error']}")
                return []

            return data.get("organic_results", [])

        except Exception:
            logger.exception(f"SerpApi raw search failed for query: {query}")
            return []