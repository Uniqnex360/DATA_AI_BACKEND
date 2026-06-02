import logging
from typing import List, Optional
from urllib.parse import urlparse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func
from sqlmodel import select
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential
from app.aggregation.services.search_service import SerpApiSearchService
from app.aggregation.services.download_service import HttpDownloadService
from app.core.config import settings
from app.models.brand import Brand
logger = logging.getLogger("product_discovery")
class ProductDiscoveryService:
    def __init__(self, max_results: int = 5):
        self.search_service = SerpApiSearchService(max_results=max_results)
        self.download_service = HttpDownloadService(timeout=20)
    @staticmethod
    def _clean_url(url: str) -> str:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))

    @staticmethod
    def _exact_mpn_match(text: str, mpn: str) -> bool:
        import re
        text = (text or "").lower()
        mpn = (mpn or "").lower().replace("‑", "-").replace("–", "-").replace("—", "-").strip()
        if not text or not mpn:
            return False
        tokens = re.findall(r"[a-z0-9]+", mpn)
        if not tokens:
            return False
        if len(tokens) == 1:
            return re.search(rf"(?<![a-z0-9]){re.escape(tokens[0])}(?![a-z0-9])", text) is not None
        return all(re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", text) for t in tokens)

    async def discover_manufacturer_domain(
        self,
        brand: str,
        category: Optional[str] = None,
        db: Optional[AsyncSession] = None 
    ) -> Optional[str]:
        brand_record = None
        if db:
            stmt = select(Brand).where(func.lower(Brand.name) == func.lower(brand.strip()))
            result = await db.execute(stmt)
            brand_record = result.scalars().first()
            if brand_record and brand_record.website:
                cached_url = brand_record.website
                if not cached_url.startswith("http"):
                    cached_url = f"https://{cached_url}"
                logger.info(f"✓ Manufacturer domain found in DB : {cached_url}")
                return cached_url
        query = f"{brand} official website"
        if category:
            main_category = category.split(" > ")[-1].strip() if " > " in category else category.strip()
            query = f"{brand} {main_category} official website"
        logger.info(f"[Manufacturer Discovery] Query: {query}")
        results = await self.search_service.search(query=query)
        discovered_url = None

        for result in results:
            url = result.get("link")

            if not url:
                continue

            domain = urlparse(url).netloc.lower().replace("www.", "")
            brand_base = brand.lower().replace(" ", "")

            logger.info(f"Checking domain: {domain}")

            # Skip marketplaces
            if any(x in domain for x in ["amazon.com", "walmart.com", "ebay.com"]):
                continue

            if domain.startswith(brand_base):
                discovered_url = f"https://{domain}"
                logger.info(f"✓ Manufacturer domain found via search: {discovered_url}")
                break

        if discovered_url and db and brand_record:
            try:
                brand_record.website = discovered_url
                db.add(brand_record)
                await db.commit()
                logger.info(f"✓ Saved manufacturer domain to DB  for {brand}")
            except Exception as e:
                logger.warning(f"Failed to save manufacturer domain to DB: {e}")
                await db.rollback()
        elif discovered_url and db and not brand_record:
            try:
                new_brand = Brand(
                    name=brand.strip(),
                    normalized_name=brand.strip().lower(),
                    website=discovered_url
                )
                db.add(new_brand)
                await db.commit()
                logger.info(f"✓ Created new Brand record and cached domain for {brand}")
            except Exception as e:
                logger.warning(f"Failed to create new Brand record: {e}")
                await db.rollback()
        if not discovered_url:
            logger.warning("No manufacturer domain found")
        return discovered_url
    async def find_product_page(
    self,
    domain: str,
    brand: str,
    mpn: str,
    title: Optional[str] = None
) -> Optional[str]:
        clean_domain = urlparse(domain).netloc
        query = f"{brand} {mpn} site:{clean_domain}"
        logger.info(f"[Product Search] Query: {query}")
        
        results = await self.search_service.search(query=query)
        
        mpn_normalized = mpn.lower().replace("‑", "-").replace("–", "-").replace("—", "-").strip()
        mpn_url_safe = mpn_normalized.replace(" ", "-")
        
        # ★ CATEGORIZE URLs
        mpm_matching_urls = []
        product_page_urls = []
        other_urls = []
        
        for result in results:
            url = result.get("link")
            logger.info(f"🔍 SERP result URL: {url}")
            if not url or clean_domain not in url:
                continue
            
            url_lower = url.lower()
            
            path = urlparse(url).path.lower()
            if self._exact_mpn_match(path, mpn):
                mpm_matching_urls.append(url)
                logger.info(f"✓ Found MPN in URL: {url}")
            elif any(x in url_lower for x in ["/product/", "/products/", "/item/"]):
                product_page_urls.append(url)
            else:
                other_urls.append(url)
        
        # ★ VARIANT KEYWORDS
        variant_keywords = {
            'kit': 10,
            '-14': 9,
            '-8ah': 8,
            '-combo': 10,
            'bare': 2,
            'refurb': 6,
            'with-battery': 8,
            'with-stand': 5,
            'bare-tool': 2,
            'bundle': 10,
            'combo': 10,
            'set': 8,
            'pack': 8,
        }
        
        def url_variant_score(url):
            url_lower = url.lower()
            score = 0
            for keyword, penalty in variant_keywords.items():
                if keyword in url_lower:
                    score += penalty
            return score
        
        # ★ SORT BY VARIANT SCORE
        mpm_matching_urls.sort(key=url_variant_score)
        
        candidate_urls = mpm_matching_urls + product_page_urls + other_urls
        
        # ★ VERIFY AND RETURN
        best_url = None
        best_score = 0
        
        for url in candidate_urls:
            verification = await self.verify_product_page(
                url=url,
                brand=brand,
                mpn=mpn
            )
            if verification["is_valid"]:
                logger.info(
                    f"✓ Verified product page ({verification['score']}): {url}"
                )
                return url  
            if verification["score"] > best_score:
                best_score = verification["score"]
                best_url = url
        
        if best_score >= 40:
            logger.warning(
                f"Using fallback product page ({best_score}): {best_url}"
            )
            return best_url
        
        logger.warning("No verified product page found")
        return None
    async def verify_product_page(
        self,
        url: str,
        brand: str,
        mpn: str,
        upc: str = None
    ) -> dict:
        path = urlparse(url).path.lower()
        category_patterns = [
            "/collections/",
            "/category/",
            "/shop/",
            "/browse/",
            "/items/",
            "/locations/",   # Block Fastenal locations
            "/branches/",    # Block other store locators
            "/stores/",      # Block store lists
        ]
        is_category_page = any(pattern in path for pattern in category_patterns)
        product_patterns = [
        "/product/",
        "/products/[a-z0-9-]+/?$",  
        "/sku/",
        "/item/",
        "/p/",
        "/-p-", 
    ]
        is_product_page = any(pattern in path for pattern in product_patterns)
        if "/products/" in path:
            path_parts = [p for p in path.split('/') if p]
            # If there's something after 'products', treat it as a potential product
            if len(path_parts) > 1:
                is_product_page = True
            else:
                is_category_page = True
        if is_category_page and not is_product_page:
            logger.info(f"Rejecting category/listing page: {url}")
            return {"is_valid": False, "score": 0}
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(2),
                wait=wait_exponential(multiplier=1, min=1, max=4),
            ):
                with attempt:
                    content = await self.download_service.download(url)
                    
                    
            
            if not content or content.get("type") != "html":
                return {"is_valid": False, "score": 0}
            
            html = content["raw_bytes"].decode("utf-8", errors="ignore").lower()
            logger.info(f"🔍 URL: {url}")
            brand_lower = brand.lower() if brand else ""
            mpn_lower = mpn.lower() if mpn else ""
            upc_lower = upc.lower() if upc else ""
            logger.info(f"🔍 Content type: {content.get('type')}")
            mpn_normalized = mpn_lower.replace("‑", "-").replace("–", "-").replace("—", "-").strip()
            
            has_brand = brand_lower in html if brand_lower else False
            logger.info(f"🔍 Brand '{brand_lower}' in HTML: {has_brand}")
            
            has_mpn = False
            if mpn_lower:
                # has_mpn_exact = mpn_lower in html
                # has_mpn_normalized = mpn_normalized in html if mpn_normalized != mpn_lower else False
                # mpn_no_dashes = mpn_normalized.replace("-", "")
                # has_mpn_no_dashes = mpn_no_dashes in html
                
                # has_mpn = has_mpn_exact or has_mpn_normalized or has_mpn_no_dashes
                has_mpn = self._exact_mpn_match(html, mpn) or self._exact_mpn_match(path, mpn)

                logger.info(f"🔍 MPN found in HTML (exact match): {self._exact_mpn_match(html, mpn)}")
                logger.info(f"🔍 MPN found in URL path (exact match): {self._exact_mpn_match(path, mpn)}")
                logger.info(f"🔍 MPN found (any method): {has_mpn}")
            
            has_upc = upc_lower in html if upc_lower else False
            
            score = 0
            if has_brand:
                score += 30
            if has_mpn:
                score += 50
            if has_upc:
                score += 20
            
            ecommerce_indicators = [
                "add to cart",
                "buy now",
                "price",
                "sku",
                "specifications",
                "product details",
                "$",
                "€",
                "£"
            ]
            indicator_score = sum(
                1 for ind in ecommerce_indicators if ind in html
            )
            if indicator_score >= 2:
                score += 10
            
            parsed = urlparse(url)
            is_homepage = parsed.path.strip("/") == ""
            if is_homepage and not has_mpn:
                score -= 40
            
            score = max(0, min(score, 100))
            is_valid = score >= 60
            
            logger.debug(
                f"Verification for {url}: brand={has_brand}, mpn={has_mpn}, upc={has_upc}, score={score}"
            )
            
            return {
                "is_valid": is_valid,
                "score": score,
                "brand_found": has_brand,
                "mpn_found": has_mpn,
                "upc_found": has_upc
            }
        except Exception as e:
            logger.warning(f"Verification failed for {url}: {e}")
            return {"is_valid": False, "score": 0}