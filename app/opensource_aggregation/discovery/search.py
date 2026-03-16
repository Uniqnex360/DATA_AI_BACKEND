# import httpx
# import logging
# from typing import List, Optional
# from urllib.parse import urlparse

# from app.opensource_aggregation.models.schemas import ProductIdentifier, SourceType
# from app.opensource_aggregation.config import config

# logger = logging.getLogger("os_discovery")

# # Blocked domains (login walls, no useful data)
# BLOCKED_DOMAINS = [
#      "google.com","youtube.com", "facebook.com", "twitter.com", "linkedin.com",
#     "pinterest.com", "instagram.com", "reddit.com", "wikipedia.org",
#     "ebay.com"
# ]


# class SourceDiscovery:
#     """Discover product data sources using SerpAPI or direct search"""

#     def __init__(self, serp_api_key: Optional[str] = None):
#         self.serp_api_key = serp_api_key
#         self.client = httpx.AsyncClient(timeout=config.search_timeout)

#     async def discover_sources(self, product: ProductIdentifier) -> List[dict]:
#         """
#         Find URLs containing product information

#         Returns list of {'url': str, 'source_type': SourceType}
#         """
#         urls = []

#         if self.serp_api_key:
#             urls = await self._search_serp(product)
#         else:
#             urls = await self._search_fallback(product)

#         # Classify and filter
#         classified = []
#         seen_domains = set()

#         for url in urls:
#             domain = urlparse(url).netloc.replace("www.", "")

#             # Skip blocked domains
#             if any(blocked in domain for blocked in BLOCKED_DOMAINS):
#                 continue

#             # Skip duplicate domains
#             if domain in seen_domains:
#                 continue
#             seen_domains.add(domain)

#             source_type = self._classify_source(domain)
#             classified.append({
#                 'url': url,
#                 'source_type': source_type,
#                 'domain': domain
#             })

#         # Sort: manufacturer first, then distributor, then retail
#         priority = {
#             SourceType.MANUFACTURER: 0,
#             SourceType.PDF_MANUAL: 1,
#             SourceType.DISTRIBUTOR: 2,
#             SourceType.RETAIL: 3,
#             SourceType.UNKNOWN: 4
#         }
#         classified.sort(key=lambda x: priority.get(x['source_type'], 5))

#         logger.info(f"🔍 Discovered {len(classified)} sources for {product.mpn}")
#         return classified[:config.max_sources_to_extract]

#     async def _search_serp(self, product: ProductIdentifier) -> List[str]:
#         """Search using SerpAPI"""
#         query = f"{product.brand} {product.mpn}".strip()

#         try:
#             response = await self.client.get(
#                 "https://serpapi.com/search",
#                 params={
#                     "engine": "google",
#                     "q": query,
#                     "api_key": self.serp_api_key,
#                     "num": config.max_search_results
#                 }
#             )
#             data = response.json()
#             urls = []
#             for result in data.get("organic_results", []):
#                 url = result.get("link")
#                 if url:
#                     urls.append(url)
#             return urls

#         except Exception as e:
#             logger.error(f"SerpAPI search failed: {e}")
#             return []

#     async def _search_fallback(self, product: ProductIdentifier) -> List[str]:
#         """Improved fallback discovery when SerpAPI is not available"""
#         urls = []
#         brand = (product.brand or "").strip()
#         mpn = (product.mpn or "").strip()

#         if not mpn:
#             return urls

#         # Stronger fallback patterns
#         search_terms = [
#             f"{brand} {mpn}",
#             mpn,
#             f"{brand} {mpn} datasheet",
#             f"{brand} {mpn} manual",
#             f"{brand} {mpn} specifications"
#         ]

#         # Common manufacturer URL patterns
#         patterns = [
#             f"https://www.{brand.lower().replace(' ', '')}.com",
#             f"https://www.{brand.lower().replace(' ', '')}.com/products",
#             f"https://www.{brand.lower().replace(' ', '')}.com/product",
#         ]

#         for term in search_terms:
#             # Try Google-like direct product search simulation
#             query = term.replace(" ", "+")

#             # Try direct manufacturer product pages
#             for base in patterns:
#                 urls.append(f"{base}/{mpn}")
#                 urls.append(f"{base}/product/{mpn}")
#                 urls.append(f"{base}/items/{mpn}")

#         # Remove duplicates
#         seen = set()
#         clean_urls = []
#         for url in urls:
#             if url not in seen:
#                 seen.add(url)
#                 clean_urls.append(url)

#         logger.info(f"Fallback discovery generated {len(clean_urls)} potential URLs for {mpn}")
#         return clean_urls[:15]  # Limit to reasonable number

#     def _classify_source(self, domain: str) -> SourceType:
#         """Classify a URL by its domain"""
#         domain_lower = domain.lower()

#         for mfg in config.manufacturer_domains:
#             if mfg in domain_lower:
#                 return SourceType.MANUFACTURER

#         for dist in config.distributor_domains:
#             if dist in domain_lower:
#                 return SourceType.DISTRIBUTOR

#         for retail in config.retail_domains:
#             if retail in domain_lower:
#                 return SourceType.RETAIL

#         if ".pdf" in domain_lower:
#             return SourceType.PDF_MANUAL

#         return SourceType.UNKNOWN

#     async def close(self):
#         await self.client.aclose()
import httpx
import logging
from typing import List, Optional
from urllib.parse import urlparse

from app.opensource_aggregation.models.schemas import ProductIdentifier, SourceType
from app.opensource_aggregation.config import config

logger = logging.getLogger("os_discovery")

# Blocked domains (login walls, search engines, no useful data)
BLOCKED_DOMAINS = [
    "google.com",           # ← added
    "youtube.com",
    "facebook.com",
    "twitter.com",
    "linkedin.com",
    "pinterest.com",
    "instagram.com",
    "reddit.com",
    "wikipedia.org",
    "ebay.com"
]


class SourceDiscovery:
    """Discover product data sources using SerpAPI or direct search"""

    def __init__(self, serp_api_key: Optional[str] = None):
        self.serp_api_key = serp_api_key
        self.client = httpx.AsyncClient(timeout=config.search_timeout)

    async def discover_sources(self, product: ProductIdentifier) -> List[dict]:
        """
        Find URLs containing product information

        Returns list of {'url': str, 'source_type': SourceType, 'domain': str}
        """
        urls = []

        if self.serp_api_key:
            urls = await self._search_serp(product)
        else:
            urls = await self._search_fallback(product)

        # Classify and filter
        classified = []
        seen_domains = set()

        for url in urls:
            domain = urlparse(url).netloc.replace("www.", "")

            # Skip blocked domains
            if any(blocked in domain for blocked in BLOCKED_DOMAINS):
                continue

            # Skip duplicate domains (take first occurrence)
            if domain in seen_domains:
                continue
            seen_domains.add(domain)

            source_type = self._classify_source(domain)
            classified.append({
                'url': url,
                'source_type': source_type,
                'domain': domain
            })

        # Sort: manufacturer first, then PDF, distributor, retail, unknown
        priority = {
            SourceType.MANUFACTURER: 0,
            SourceType.PDF_MANUAL: 1,
            SourceType.DISTRIBUTOR: 2,
            SourceType.RETAIL: 3,
            SourceType.UNKNOWN: 4
        }
        classified.sort(key=lambda x: priority.get(x['source_type'], 5))

        logger.info(f"🔍 Discovered {len(classified)} sources for {product.mpn}")
        return classified[:config.max_sources_to_extract]

    async def _search_serp(self, product: ProductIdentifier) -> List[str]:
        """Search using SerpAPI (requires API key)"""
        query = f"{product.brand} {product.mpn}".strip()
        try:
            response = await self.client.get(
                "https://serpapi.com/search",
                params={
                    "engine": "google",
                    "q": query,
                    "api_key": self.serp_api_key,
                    "num": config.max_search_results
                }
            )
            data = response.json()
            urls = []
            for result in data.get("organic_results", []):
                url = result.get("link")
                if url:
                    urls.append(url)
            return urls
        except Exception as e:
            logger.error(f"SerpAPI search failed: {e}")
            return []

    async def _search_fallback(self, product: ProductIdentifier) -> List[str]:
        """Fallback discovery when SerpAPI is not available (no Google URLs)"""
        urls = []
        brand = (product.brand or "").strip()
        mpn = (product.mpn or "").strip()

        if not mpn:
            return urls

        # Generate potential manufacturer product page URLs
        base_domain = brand.lower().replace(' ', '').replace('.', '')
        patterns = [
            f"https://www.{base_domain}.com/product/{mpn}",
            f"https://www.{base_domain}.com/products/{mpn}",
            f"https://www.{base_domain}.com/{mpn}",
            f"https://www.{base_domain}.com/item/{mpn}",
            f"https://www.{base_domain}.com/p/{mpn}",
        ]

        # Also try adding "datasheet" or "specs" if they lead to PDFs
        patterns.append(f"https://www.{base_domain}.com/content/dam/{mpn}.pdf")
        patterns.append(f"https://www.{base_domain}.com/specs/{mpn}.pdf")

        # Add distributor / retailer patterns (optional, can be expanded)
        distributor_domains = ["digikey.com", "mouser.com", "newark.com", "rsdelivers.com"]
        for dist in distributor_domains:
            patterns.append(f"https://www.{dist}/product/{brand}/{mpn}")

        for url in patterns:
            urls.append(url)

        # Remove duplicates
        seen = set()
        clean_urls = []
        for url in urls:
            if url not in seen:
                seen.add(url)
                clean_urls.append(url)

        logger.info(f"Fallback discovery generated {len(clean_urls)} potential URLs for {mpn}")
        return clean_urls[:15]

    def _classify_source(self, domain: str) -> SourceType:
        """Classify a URL by its domain"""
        domain_lower = domain.lower()

        for mfg in config.manufacturer_domains:
            if mfg in domain_lower:
                return SourceType.MANUFACTURER

        for dist in config.distributor_domains:
            if dist in domain_lower:
                return SourceType.DISTRIBUTOR

        for retail in config.retail_domains:
            if retail in domain_lower:
                return SourceType.RETAIL

        if domain_lower.endswith('.pdf'):
            return SourceType.PDF_MANUAL

        return SourceType.UNKNOWN

    async def close(self):
        await self.client.aclose()