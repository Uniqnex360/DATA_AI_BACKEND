import logging
from typing import List, Optional
from urllib.parse import urlparse
from curl_cffi import AsyncSession
from sqlalchemy import func
from sqlmodel import select
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

from app.aggregation.services.search_service import SerpApiSearchService
from app.aggregation.services.download_service import HttpDownloadService
from app.models.brand import Brand

logger = logging.getLogger("product_discovery")


class ProductDiscoveryService:

    def __init__(self, max_results: int = 5):
        self.search_service = SerpApiSearchService(max_results=max_results)
        self.download_service = HttpDownloadService(timeout=20)

    # ✅ 1. Discover Manufacturer Domain
    async def discover_manufacturer_domain(
        self,
        brand: str,
        category: Optional[str] = None,
        db: Optional[AsyncSession] = None 
    ) -> Optional[str]:
        
        brand_record = None
        
        # 🚀 STEP 1: CHECK DATABASE CACHE FIRST
        if db:
            stmt = select(Brand).where(func.lower(Brand.name) == func.lower(brand.strip()))
            result = await db.execute(stmt)
            brand_record = result.scalars().first()
            
            if brand_record and brand_record.website:
                cached_url = brand_record.website
                if not cached_url.startswith("http"):
                    cached_url = f"https://{cached_url}"
                logger.info(f"✓ Manufacturer domain found in DB cache: {cached_url}")
                return cached_url

        # 🌐 STEP 2: SEARCH VIA API (Fallback if not in DB)
        query = f"{brand} official website"
        if category:
            # Use main category to avoid overly long queries
            main_category = category.split(" > ")[-1].strip() if " > " in category else category.strip()
            query = f"{brand} {main_category} official website"

        logger.info(f"[Manufacturer Discovery] Query: {query}")
        results = await self.search_service.search(query=query)

        discovered_url = None
        for result in results:
            url = result.get("link") or result.get("url", "")
            if not url:
                continue

            domain = urlparse(url).netloc.lower()

            # Must contain brand name in domain
            if brand.lower().replace(" ", "") in domain:
                discovered_url = f"https://{domain}"
                logger.info(f"✓ Manufacturer domain found via search: {discovered_url}")
                break

        # 💾 STEP 3: SAVE TO DATABASE CACHE
        if discovered_url and db and brand_record:
            try:
                brand_record.website = discovered_url
                db.add(brand_record)
                await db.commit()
                logger.info(f"✓ Saved manufacturer domain to DB cache for {brand}")
            except Exception as e:
                logger.warning(f"Failed to save manufacturer domain to DB: {e}")
                await db.rollback()
                
        elif discovered_url and db and not brand_record:
            # Optional: Create the brand record if it doesn't exist yet
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

    # ✅ 2. Find Product Page via site: search
    # ✅ 2. Find Product Page via site: search
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

        candidate_urls = []

        for result in results:
            url = result.get("link")
            if not url:
                continue

            if clean_domain not in url:
                continue

            url_lower = url.lower()

            # Prioritize likely product URLs
            if any(x in url_lower for x in ["/product/", "/products/", "/item/"]):
                candidate_urls.insert(0, url)
            else:
                candidate_urls.append(url)

        # ✅ Verify candidates properly
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
                return url  # Immediate return on first valid

            # Keep best scoring fallback
            if verification["score"] > best_score:
                best_score = verification["score"]
                best_url = url

        # Optional: fallback if moderately confident
        if best_score >= 40:
            logger.warning(
                f"Using fallback product page ({best_score}): {best_url}"
            )
            return best_url

        logger.warning("No verified product page found")
        return None

    # ✅ 3. Verify Product Page Content
    async def verify_product_page(self,url: str,brand: str,mpn: str,upc: str = None) -> dict:

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

            brand_lower = brand.lower() if brand else ""
            mpn_lower = mpn.lower() if mpn else ""
            upc_lower = upc.lower() if upc else ""

            has_brand = brand_lower in html if brand_lower else False
            has_mpn = mpn_lower in html if mpn_lower else False
            has_upc = upc_lower in html if upc_lower else False

            score = 0

            # ✅ Brand scoring
            if has_brand:
                score += 30

            # ✅ MPN scoring
            if has_mpn:
                score += 50

            # ✅ UPC scoring
            if has_upc:
                score += 20

            # ✅ Ecommerce indicators
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

            # ✅ Homepage penalty
            parsed = urlparse(url)
            is_homepage = parsed.path.strip("/") == ""

            if is_homepage and not has_mpn:
                score -= 40

            # Clamp score
            score = max(0, min(score, 100))

            # ✅ Define validity threshold
            is_valid = score >= 60

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