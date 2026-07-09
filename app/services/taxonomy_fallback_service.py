

import logging
import re
from typing import List, Optional, Set, Dict, Any
from urllib.parse import urlparse
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.aggregation.services.smart_search import SmartSearchService
from app.models.product import Product
from app.services.product_discovery_service import ProductDiscoveryService

logger = logging.getLogger("taxonomy_fallback")


@dataclass
class TaxonomySource:
    """Represents a successful source domain from a taxonomy peer."""
    domain: str
    success_count: int
    avg_completeness: float
    last_used: Optional[datetime]
    sample_urls: List[str]


class TaxonomyFallbackService:
    """
    Fallback service that leverages historically successful sources 
    from products in the same taxonomy/category.
    """

    BLOCKED_DOMAINS = {
        'wikipedia.org', 'youtube.com', 'youtu.be',
        'facebook.com', 'twitter.com', 'instagram.com',
        'reddit.com', 'quora.com', 'amazon.com',
        'ebay.com', 'walmart.com', 'aliexpress.com',
        'tiktok.com', 'pinterest.com', 'linkedin.com'
    }

    def __init__(
        self,
        db: AsyncSession,
        search_service: SmartSearchService,
        discovery_service: ProductDiscoveryService,
        max_domains: int = 5,
        min_completeness: float = 0.6,
        max_results_per_domain: int = 2
    ):
        self.db = db
        self.search_service = search_service
        self.discovery_service = discovery_service
        self.max_domains = max_domains
        self.min_completeness = min_completeness
        self.max_results_per_domain = max_results_per_domain

    async def find_urls(
        self,
        brand: str,
        title: str,
        mpn: str,
        taxonomy: str,
        validation_callback: Optional[callable] = None
    ) -> List[str]:
        """
        Main entry point: Find URLs using taxonomy peer sources.
        
        Args:
            brand: Product brand
            title: Product title  
            mpn: Manufacturer part number
            taxonomy: Product taxonomy/category
            validation_callback: Optional function(url, snippet) -> bool for validation
            
        Returns:
            List of URLs found from taxonomy peer domains
        """
        if not taxonomy or not self.db:
            logger.info(
                "No taxonomy or DB session provided, skipping taxonomy fallback")
            return []

        logger.info(
            f"Starting taxonomy fallback for {mpn} in category: {taxonomy}")

        
        sources = await self._get_taxonomy_sources(taxonomy)

        if not sources:
            logger.info(f"No taxonomy peer sources found for: {taxonomy}")
            return []

        logger.info(
            f"Found {len(sources)} potential domains from {sum(s.success_count for s in sources)} historical successes")

        
        found_urls = []
        for source in sources:
            if len(found_urls) >= 3:  
                break

            try:
                urls = await self._search_domain(
                    source=source,
                    brand=brand,
                    title=title,
                    mpn=mpn,
                    validation_callback=validation_callback
                )
                found_urls.extend(urls)

            except Exception as e:
                logger.error(f"Error searching domain {source.domain}: {e}")
                continue

        
        seen = set()
        unique_urls = [url for url in found_urls if not (
            url in seen or seen.add(url))]

        logger.info(
            f"Taxonomy fallback complete for {mpn}: {len(unique_urls)} URLs found")
        return unique_urls[:3]  

    async def _get_taxonomy_sources(self, taxonomy: str) -> List[TaxonomySource]:
        """
        Query database for successful products in same taxonomy.
        Returns domains sorted by success frequency and quality.
        """
        try:
            
            
            tax_parts = [t.strip() for t in taxonomy.split('>')]
            search_patterns = [
                taxonomy,  
                tax_parts[0] if tax_parts else taxonomy,  
                ' > '.join(tax_parts[:2]) if len(
                    tax_parts) >= 2 else taxonomy  
            ]

            stmt = select(
                Product.sources_consulted,
                Product.completeness_score,
                Product.updated_at,
                Product.product_code
            ).where(
                Product.taxonomy.in_(search_patterns),
                Product.enrichment_status == "completed",
                Product.sources_consulted.isnot(None),
                Product.completeness_score >= self.min_completeness
            ).order_by(
                desc(Product.completeness_score),
                desc(Product.updated_at)
            ).limit(20)  

            result = await self.db.execute(stmt)
            rows = result.all()

            if not rows:
                return []

            
            domain_stats: Dict[str, Dict] = {}

            for row in rows:
                sources = row[0] or []
                completeness = row[1] or 0.0
                updated_at = row[2]
                product_code = row[3]

                for url in sources:
                    if not url or not isinstance(url, str):
                        continue

                    try:
                        parsed = urlparse(url)
                        domain = parsed.netloc.lower()

                        
                        if any(blocked in domain for blocked in self.BLOCKED_DOMAINS):
                            continue

                        
                        if '.' not in domain or len(domain) < 4:
                            continue

                        if domain not in domain_stats:
                            domain_stats[domain] = {
                                'count': 0,
                                'completeness_sum': 0.0,
                                'last_used': updated_at,
                                'sample_urls': []
                            }

                        domain_stats[domain]['count'] += 1
                        domain_stats[domain]['completeness_sum'] += completeness

                        
                        if len(domain_stats[domain]['sample_urls']) < 3:
                            domain_stats[domain]['sample_urls'].append(url)

                    except Exception:
                        continue

            
            sources = []
            for domain, stats in domain_stats.items():
                sources.append(TaxonomySource(
                    domain=domain,
                    success_count=stats['count'],
                    avg_completeness=stats['completeness_sum'] /
                    stats['count'],
                    last_used=stats['last_used'],
                    sample_urls=stats['sample_urls']
                ))

            
            sources.sort(key=lambda x: (x.success_count *
                         x.avg_completeness), reverse=True)

            return sources[:self.max_domains]

        except Exception as e:
            logger.error(f"Database error querying taxonomy sources: {e}")
            return []

    async def _search_domain(
        self,
        source: TaxonomySource,
        brand: str,
        title: str,
        mpn: str,
        validation_callback: Optional[callable] = None
    ) -> List[str]:
        """
        Search for product on a specific domain that worked for taxonomy peers.
        """
        domain = source.domain
        found_urls = []

        logger.info(
            f"Searching taxonomy domain: {domain} (historical score: {source.avg_completeness:.2f})")

        try:
            
            if source.sample_urls:
                
                sample_path = urlparse(source.sample_urls[0]).path
                
                if any(pattern in sample_path for pattern in ['/product', '/item', '/p/']):
                    product_url = await self.discovery_service.find_product_page(
                        domain=f"https://{domain}",
                        brand=brand,
                        mpn=mpn,
                        title=title
                    )

                    if product_url and self._is_valid_result(product_url, brand, validation_callback):
                        logger.info(
                            f"✓ Found via discovery on taxonomy domain {domain}: {product_url}")
                        return [product_url]

            
            
            clean_title = self._clean_title(title)
            
            search_query = f"{brand} {mpn} {clean_title} site:{domain}"

            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                response = await client.get(
                    f"{settings.SEARXNG_URL}/search",
                    params={
                        "q": search_query,
                        "format": "json",
                        "categories": "general",
                        "language": "en",
                        "safesearch": "0"
                    }
                )

                if response.status_code != 200:
                    logger.warning(
                        f"Search failed for {domain}: HTTP {response.status_code}")
                    return []

                data = response.json()
                results = data.get("results", [])

                for r in results[:self.max_results_per_domain]:
                    url = r.get("url", "")
                    snippet = f"{r.get('title', '')} {r.get('content', '')}"

                    if not url:
                        continue

                    
                    if not self._is_valid_result(url, brand, validation_callback, snippet):
                        continue

                    
                    if not self.search_service.is_likely_pdp_url(url):
                        continue

                    found_urls.append(url)
                    logger.info(
                        f"✓ Found via search on taxonomy domain {domain}: {url}")

                    if len(found_urls) >= self.max_results_per_domain:
                        break

        except httpx.TimeoutException:
            logger.warning(f"Timeout searching taxonomy domain: {domain}")
        except Exception as e:
            logger.error(f"Error searching taxonomy domain {domain}: {e}")

        return found_urls

    def _is_valid_result(
        self,
        url: str,
        brand: str,
        validation_callback: Optional[callable] = None,
        snippet: str = ""
    ) -> bool:
        """Validate that result is relevant."""
        
        if not url or not isinstance(url, str):
            return False

        
        brand_variants = [brand.lower(), brand.lower().replace(
            " ", ""), brand.lower().replace(" ", "-")]
        url_lower = url.lower()
        snippet_lower = snippet.lower()

        has_brand = any(
            variant in url_lower or variant in snippet_lower for variant in brand_variants)

        if not has_brand:
            logger.info(f"Rejecting {url}: Brand '{brand}' not found")
            return False

        
        if validation_callback and not validation_callback(url, snippet):
            return False

        return True

    def _clean_title(self, title: str) -> str:
        """Clean title for search query."""
        if not title:
            return ""

        
        stop_words = {'the', 'a', 'an', 'and', 'or',
                      'but', 'with', 'for', 'from', 'to', 'of', 'in'}
        words = title.split()
        cleaned = [w for w in words if w.lower() not in stop_words]

        
        return " ".join(cleaned[:6])  



async def taxonomy_fallback_search(
    brand: str,
    title: str,
    mpn: str,
    taxonomy: str,
    db: AsyncSession,
    search_service: SmartSearchService,
    discovery_service: ProductDiscoveryService,
    validation_callback: Optional[callable] = None
) -> List[str]:
    """
    Standalone convenience function for taxonomy fallback.
    
    Example usage:
        urls = await taxonomy_fallback_search(
            brand="LickiMat",
            title="Classic Soother Slow Feeder",
            mpn="4459",
            taxonomy="Dog > Feeding & Watering Supplies > Feeding Mats",
            db=db,
            search_service=search_service,
            discovery_service=discovery_service,
            validation_callback=lambda url, snippet: "classic" in snippet.lower()
        )
    """
    service = TaxonomyFallbackService(
        db=db,
        search_service=search_service,
        discovery_service=discovery_service
    )

    return await service.find_urls(
        brand=brand,
        title=title,
        mpn=mpn,
        taxonomy=taxonomy,
        validation_callback=validation_callback
    )
