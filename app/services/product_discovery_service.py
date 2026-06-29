import logging
from typing import  Optional
from urllib.parse import urlparse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func
from sqlmodel import select
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential
from app.aggregation.services.search_service import SerpApiSearchService
from app.aggregation.services.download_service import HttpDownloadService
from app.models.brand import Brand
import re
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
        if not mpn or str(mpn).lower() in ['none', 'null', 'nan']:
            return False
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
        db: Optional[AsyncSession] = None,
        title:Optional[str]=None
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
                logger.info(f"Created new Brand record and cached domain for {brand}")
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
    title: Optional[str] = None,
    taxonomy:Optional[str]=None,
) -> Optional[str]:
        clean_domain = urlparse(domain).netloc
        is_mpn_valid = mpn and str(mpn).strip().lower() != 'none'
        target_type = taxonomy.split(" > ")[-1].strip() if taxonomy else None
        exclusion_str = ""
        if taxonomy and target_type:
            target_base = target_type.lower().rstrip("s")
            parts = [p.strip() for p in taxonomy.split(">")]
            for part in parts:
                if "&" in part or " and " in part.lower():
                    siblings = re.split(
                        r'\s*[&]|(?:and)\s*', part, flags=re.IGNORECASE)
                    for sibling in siblings:
                        sibling_base = sibling.strip().lower().rstrip("s")
                        if sibling_base and sibling_base != target_base:
                            exclusion_str += f" -{sibling.strip().lower()}"

        clean_title = SerpApiSearchService._clean_search_query(
            title) if title else ""
        if is_mpn_valid:
            # queries = [f"{brand} {mpn} site:{clean_domain}{exclusion_str}"]
            queries = [f"{brand} {mpn} site:{clean_domain}"] 
        else:
            queries = [
                f"{brand} {clean_title} site:{clean_domain}{exclusion_str}",
                f"{brand} {' '.join(clean_title.split()[:3])} site:{clean_domain}{exclusion_str}"
            ]

        results = []
        for q in queries:
            logger.info(f"[Product Search] Query: {q}")
            results = await self.search_service.search(query=q)
            if results:
                break
        mpn_normalized = (mpn or "").lower().replace("‑", "-").replace("–", "-").replace("—", "-").strip()
        mpn_url_safe = mpn_normalized.replace(" ", "-")
        mpm_matching_urls = []
        product_page_urls = []
        other_urls = []
        for result in results:
            url = result.get("link")
            logger.info(f" SERP result URL: {url}")
            if not url or clean_domain not in url:
                continue
            query_string = urlparse(url).query.lower()
            SEARCH_QUERY_PATTERNS = ["q=", "query=", "searchterm=", "keyword=", "pagesize=", "page="]
            if any(p in query_string for p in SEARCH_QUERY_PATTERNS):
                logger.info(f"Skipping search/pagination URL: {url}")
                continue
            path = urlparse(url).path.lower()
            if target_type and exclusion_str:
                is_target_in_url = target_type.lower() in path
                has_sibling_in_url = any(excl.replace(
                    "-", "") in path for excl in exclusion_str.split())
                if has_sibling_in_url and not is_target_in_url:
                    logger.info(f"Skipping sibling variant URL: {url}")
                    continue

            url_lower = url.lower()
            if self._exact_mpn_match(path, mpn):
                mpm_matching_urls.append(url)
                logger.info(f"Found MPN in URL: {url}")
            elif any(x in url_lower for x in ["/product/", "/products/", "/item/"]):
                product_page_urls.append(url)
            else:
                other_urls.append(url)
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
        mpm_matching_urls.sort(key=url_variant_score)
        candidate_urls = mpm_matching_urls + product_page_urls + other_urls
        best_url = None
        best_score = 0
        for url in candidate_urls:
            verification = await self.verify_product_page(
                url=url,
                brand=brand,
                mpn=mpn,
                title=title,
                taxonomy=taxonomy
            )
            if verification["is_valid"]:
                logger.info(
                    f" Verified product page ({verification['score']}): {url}"
                )
                return url  
            if verification["score"] > best_score:
                best_score = verification["score"]
                best_url = url
        if best_score >= 60:
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
        upc: str = None,
        title: str = None ,
        taxonomy:str=None
    ) -> dict:
        path = urlparse(url).path.lower()
        query_string = urlparse(url).query.lower()
        REJECT_URL_PATTERNS = [
            "/collections/",
            "/category/",
            "/shop/",
            "/browse/",
            "/items/",
            "/locations/",
            "/branches/",
            "/stores/",
            "socketsandswitches",
            "/search",      
            "/results",     
            "/listing",     
        ]
        REJECT_QUERY_PATTERNS = [
            "q=",           
            "query=",       
            "searchterm=",  
            "keyword=",     
            "pagesize=",    
            "pageSize=",    
            "page=",        
            "productCategory=",  
        ]
        category_patterns = [
            "/collections/",
            "/category/",
            "/shop/",
            "/browse/",
            "/items/",
            "/locations/",   
            "/branches/",    
            "/stores/",   
            "socketsandswitches",
        ]
        is_category_page = any(pattern in path for pattern in category_patterns)
        is_search_page = any(pattern in query_string for pattern in REJECT_QUERY_PATTERNS)
        if is_search_page:
            logger.info(f" Rejecting search/pagination page (query params): {url}")
            return {"is_valid": False, "score": 0}
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
            logger.info(f" URL: {url}")
            brand_lower = brand.lower() if brand else ""
            mpn_lower = mpn.lower() if mpn else ""
            upc_lower = upc.lower() if upc else ""
            logger.info(f" Content type: {content.get('type')}")
            mpn_normalized = mpn_lower.replace("‑", "-").replace("–", "-").replace("—", "-").strip()
            has_brand = brand_lower in html if brand_lower else False
            logger.info(f" Brand '{brand_lower}' in HTML: {has_brand}")
            has_mpn = False
            if mpn_lower:
                has_mpn = self._exact_mpn_match(html, mpn) or self._exact_mpn_match(path, mpn)
                logger.info(f" MPN found in HTML (exact match): {self._exact_mpn_match(html, mpn)}")
                logger.info(f" MPN found in URL path (exact match): {self._exact_mpn_match(path, mpn)}")
                logger.info(f" MPN found (any method): {has_mpn}")
            has_upc = upc_lower in html if upc_lower else False
            is_mpn_valid = mpn and str(mpn).strip().lower() != 'none'
            title_words = [w for w in title.lower().split()
                           if len(w) > 2] if title else []

            match_count = sum(1 for word in title_words if (
                word in html or word in path))

            has_title_density = (match_count / len(title_words)
                                >= 0.4) if title_words else False
            tax_keywords = [w.lower() for w in (
                taxonomy or "").replace(">", " ").split() if len(w) > 3]
            has_context_match = any(
                word in html for word in tax_keywords) if tax_keywords else True
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
            score = 0
            if indicator_score >= 2:
                score += 10
            if has_brand:
                score += 30
            if is_mpn_valid:
                if has_mpn:
                    score += 50
                    
                elif has_title_density:  
                    score += 30
            else:
                if has_title_density:
                    score += 40
                if has_context_match:
                    score += 20
            if has_upc:
                score += 20
                # --- SPA / EMPTY HTML FALLBACK ---
            # Modern sites (React/Next.js) return empty HTML shells to basic HTTP requests.
            # If the HTML is empty, we must trust the URL structure instead.
            if score < 60 and not has_title_density:
                path = urlparse(url).path.lower()

                # 1. Must look like a product URL structure
                is_product_url = any(p in path for p in [
                                     '/product/', '/products/', '/item/', '/p/', '/dp/'])

                # 2. Must contain the brand in the URL path
                brand_in_url = brand and brand.lower().replace(" ", "") in path

                # 3. Must contain enough context to prove it's the RIGHT product
                # If MPN is valid and in URL, that's strong proof
                mpn_in_url = is_mpn_valid and mpn.lower().replace("‑", "-") in path

                # If MPN is not valid, we rely entirely on the title words in the URL
                title_in_url = sum(1 for w in title.lower().split() if len(
                    w) > 2 and w in path) if title else 0

                # If it looks like a product, has the brand, and has either the MPN or enough Title words
                if is_product_url and brand_in_url and (mpn_in_url or title_in_url >= 2):
                    logger.info(
                        f"SPA Fallback: Trusting URL structure over empty HTML for {url} "
                        f"(MPN in URL: {mpn_in_url}, Title words in URL: {title_in_url})"
                    )
                    score = 70  # Bypass the 60 threshold

            parsed = urlparse(url)
            is_homepage = parsed.path.strip("/") == ""
            if is_homepage and not has_mpn:
                score -= 40

                # High confidence bypass
            if not is_mpn_valid and has_brand and has_title_density and has_context_match:
                score = max(score, 80)

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