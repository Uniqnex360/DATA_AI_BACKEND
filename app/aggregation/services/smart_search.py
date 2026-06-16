import logging
from typing import Any, List, Optional,Dict
from pydantic import BaseModel, Field
from app.aggregation.interfaces import ISearchService
from app.rules.rule_engine import RuleEngine
from app.search.searxng_service import SearXNGSearchService
from sqlalchemy.ext.asyncio import AsyncSession
import asyncio
logger = logging.getLogger("smart_search")
class ScoredUrl(BaseModel):
    url: str
    score: int 
    reasoning: str
class PageMatchScore(BaseModel):
        brand_found: bool
        mpn_found: bool
        upc_found: bool
        score: int
        reasoning: str
class SimpleText(BaseModel):
    text: str
class ManufacturerScoringResponse(BaseModel):
    scored_urls: List[ScoredUrl]
    best_url: Optional[str] = None
class UrlFilterResponse(BaseModel):
    selected_urls: List[str]
class ManufacturerWebsiteResponse(BaseModel):
        manufacturer_url: str
        confidence: float
class SmartSearchResponse(BaseModel):
    selected_urls: List[str]
    candidate_image_urls: List[str] = []
class ProductPageResponse(BaseModel):
        product_url: Optional[str] = None
        confidence: float = 0.0
        reasoning: str =""
class LinkJudgeResponse(BaseModel):
    """
    Schema for the LLM to identify the best Product Detail Page (PDP) 
    from a list of search results.
    """
    best_id: str = Field(
        ..., 
        description="The ID number of the best matching result (e.g., '0', '1'). Return 'None' if no product page is found."
    )
    reasoning: str = Field(
        ..., 
        description="Brief explanation of why this link was chosen (e.g., 'Matches MPN in URL' or 'Title indicates a datasheet')."
    )
    confidence: float = Field(
        ..., 
        ge=0.0, le=1.0, 
        description="Confidence score between 0.0 and 1.0."
    )
    is_pdp: bool = Field(
        ..., 
        description="True if the link is a specific Product Detail Page, False if it is a category or search result."
    )
class ManufacturerUrl(BaseModel):
    """A single verified manufacturer URL with metadata."""
    url: str = Field(default="", description="Full manufacturer URL")
    is_official: bool = Field(
        default=False,
        description="Whether this is an official manufacturer site"
    )
    relevance_score: int = Field(
        default=0,
        ge=0,
        le=100,
        description="Relevance score 0-100"
    )
    reasoning: str = Field(
        default="",
        description="Why this URL was selected"
    )

    model_config = {
        "extra": "forbid",           # ← required for OpenAI structured output
        "json_schema_extra": {
            "additionalProperties": False  # ← explicit for clarity
        }
    }


class ManufacturerUrlResponse(BaseModel):
    """Response containing ranked manufacturer URLs."""
    verified_urls: List[ManufacturerUrl] = Field(
        default_factory=list,
        description="List of verified manufacturer URLs"
    )

    model_config = {
        "extra": "forbid"            # ← required for OpenAI structured output
    }
class NavigationResponse(BaseModel):
    """LLM response for navigation step"""
    url: str
    exact_match: bool = False
    page_type: str  # "product" | "category" | "brand" | "not_found"
    confidence: float = 0.5
    reasoning: Optional[str] = None
class TargetedQueryResponse(BaseModel):
    search_query: str
class SmartSearchService(ISearchService):
    def is_likely_pdp_url(self, url: str) -> bool:
        from urllib.parse import urlparse

        # Strip query parameters and fragments for checking
        parsed = urlparse(url)
        url_lower = (parsed.scheme + "://" +
                     parsed.netloc + parsed.path).lower()

        reject_patterns = [
            '/lighting/', '/sale', '/january-sale', '/collections/',
             '/search', '/category', '/shop/',
            '?page=',  # Keep this, but srsltid is removed
            '/stores/', '/store-locator/', '/find-a-store/',
            '/locations/', '/our-stores/',
            '/login', '/register', '/account',
            '/signin', '/signup',
            '/about', '/contact', '/faq', '/help',
            '/privacy', '/terms', '/cookies',
            '/blog/', '/news/', '/articles/',
            '/journal/', '/editorial/',
            'social-stories', 'social+stories',
            'forum', 'topic', 'thread', 'community', 'answers',
            'how-to', 'wiki', 'recipe', 'brewing', 'download', 'article',
            'member', 'trophies',
        ]

        if any(p in url_lower for p in reject_patterns):
            logger.info(
                f"  ⛔ Rejected PDP check (category/sale pattern): {url}")
            return False

        path_segments = [s for s in parsed.path.strip('/').split('/') if s]

        # Generic category keywords to reject
        generic_keywords = ['lighting', 'lanterns', 'pendant', 'wall-lights', 'ceiling-lights',
                            'outdoor', 'indoor', 'task', 'sale', 'collection', 'category',
                            'january', 'february', 'march', 'april', 'may', 'june']

        for segment in path_segments:
            if segment in generic_keywords:
                logger.info(
                    f"  ⛔ Rejected PDP check (generic keyword '{segment}'): {url}")
                return False

        # A real PDP should have at least one path segment that looks like a product slug
        # Reject empty paths or very short top-level categories
        if len(path_segments) < 1:
            return False

        logger.info(f"  ✓ Accepted PDP check: {url}")
        return True
    def __init__(
        self,
        llm_provider: str,
        db: AsyncSession,
        searxng_url: str = "http://searxng:8080",
        max_results: int = 5,
    ):
        self.db = db
        self.llm_provider = llm_provider
        self.searxng = SearXNGSearchService(
            base_url=searxng_url,
            max_results=15,
        )
        self.max_results = max_results
    async def _build_targeted_query(
        self,
        mpn: str,
        brand: str,
        sku: Optional[str] = None,
        operation_mode: str = "aggregation",
        use_case: str = "",
        brand_prompt_text: Optional[str] = None,
        category_prompt_text: Optional[str] = None,
        taxonomy: Optional[str] = None,
        selected_taxonomy: Optional[str] = None, 
        title: Optional[str] = None
    ) -> str:
        from app.aggregation.aggregate_product import call_llm_with_schema
        effective_taxonomy = selected_taxonomy if selected_taxonomy else taxonomy   
        if brand_prompt_text:
            prompt = brand_prompt_text
            prompt = prompt.replace("{brand}", brand or "")
            prompt = prompt.replace("{mpn}", mpn or "")
            logger.info(f"Using brand prompt for {brand}")
        elif category_prompt_text:
            prompt = category_prompt_text
            prompt = prompt.replace("{category}", effective_taxonomy or "")  
            prompt = prompt.replace("{brand}", brand or "")
            prompt = prompt.replace("{mpn}", mpn or "")
            logger.info(f"Using category prompt for {effective_taxonomy}")
        else:
            is_mpn_valid = mpn and str(mpn).strip().lower() != 'none' and not str(mpn).startswith('UNK-')
            identifier = f"MPN: {mpn}" if is_mpn_valid else f"Product: {title}"
            prompt = f"""
        You are a product data researcher. Given a product, generate the single best 
        Google search query to find its official specifications or datasheet page.
        Product:
        - Brand: {brand}
        - {identifier}

        Rules:
        - If no MPN is provided, use the Brand and Product Name to find the official page.
        - NEVER include the word 'None' or 'UNK' in the query.
        - The query should target the manufacturer's official site or the most authoritative 
        distributor/retailer for this type of product
        - If the MPN contains hyphens or looks like a specific kit (e.g. DCF403-1PS-NA), DO NOT 
        restrict it to a single site using the 'site:' operator. Just use the brand and MPN.
        - Keep it short and precise — brand + MPN + best site or keyword
        - Examples:
        - Dewalt power tool → "Dewalt DCF414-B-NA site:dewalt.com specifications"
        - Heli-Coil insert → "Heli-Coil 1084-10CNPF250 site:grainger.com OR site:mscdirect.com"
        - Electronics → "Sony WH-1000XM5 site:sony.com specifications"
        Return JSON: {{"search_query": "your query here"}}
        """
        try:
            result = await call_llm_with_schema(
                prompt=prompt,
                response_model="TargetedQueryResponse",
                llm_provider=self.llm_provider,
                estimated_tokens=300,
            )
            if result and result.search_query:
                logger.info(f"Targeted query for {mpn}: {result.search_query}")
                return result.search_query
        except Exception as e:
            logger.warning(f"Targeted query generation failed: {e}")
        return f"{brand} {mpn if is_mpn_valid else title} specifications" 
    async def get_urls(
        self,
        query: str,
        mpn: str,
        brand: str,
        sku: Optional[str] = None,
        operation_mode: str = "aggregation",
        use_case: str = "",
        brand_prompt_text: Optional[str] = None,
        category_prompt_text: Optional[str] = None,
        taxonomy: Optional[str] = None,
        direct_urls: Optional[List[str]] = None, 
        selected_taxonomy: Optional[str] = None,
        title: Optional[str] = None,
        
    ) -> tuple[List[str], List[str]]:
        from app.aggregation.aggregate_product import call_llm_with_schema
        
        if direct_urls:
            # Filter out category/sale pages that product_discovery mistakenly verified
            direct_urls = [
                url for url in direct_urls if self.is_likely_pdp_url(url)]
            logger.info(
                f"Using {len(direct_urls)} direct URLs for {mpn}: {direct_urls}")
            if not direct_urls:
                logger.warning(
                    "All direct URLs were rejected as category/sale pages. Falling back to search.")
            if direct_urls:
                image_urls = []

                is_mpn_valid = mpn and str(mpn).strip().lower() != 'none' and not str(mpn).startswith('UNK-')
                search_id = mpn if is_mpn_valid else title
                image_task = self.searxng.search_images(f"{brand} {search_id}")
                image_results = await image_task
                if not isinstance(image_results, Exception):
                    image_urls = list({img.get("img_src") for img in image_results if img.get("img_src")})
                if direct_urls and brand:
                    logger.info(f"BEFORE MANUFACTURER SCORING (direct): {direct_urls}, brand={brand}")
                    scored_direct = await self.llm_score_manufacturer_urls(
                        urls=direct_urls,
                        brand=brand,
                        mpn=mpn,
                        upc=sku
                    )
                    logger.info(f"AFTER MANUFACTURER SCORING (direct): {scored_direct}")
                    return scored_direct, image_urls
                else:
                    return direct_urls, image_urls
            else:
                logger.warning(
                    "All direct URLs rejected as category/sale pages. Falling back to search.")
            
        BLOCKED_DOMAINS = [
            "zhihu.com", "baidu.com", "weibo.com",
            "superuser.com", "tenforums.com", "stackoverflow.com",
            "support.google.com", "support.microsoft.com",
            "accounts.google.com", "gmail.com", "google.co.in",
            "wikipedia.org", "wikimedia.org",
            "youtube.com", "facebook.com", "twitter.com",
            "reddit.com", "quora.com",
            "people.com", "hola.com", "usatoday.com", "msn.com",
            "breakingdown.jp", "spinny.com", "wikihow.com",
            "20min.ch", "24heures.ch", "larousse.fr", "universalis.fr",
            "linux.org", "runoob.com", "linuxcool.com",
            "whatsapp.com", "wa.me", "web.whatsapp.com",
            "forum.toolsinaction.com", "toolsinaction.com",
            "forums.dewalt.com", "community.dewalt.com",
        ]
        base_query = self.searxng._build_query(mpn, brand, sku=sku)
        targeted_query_str = await self._build_targeted_query(
            mpn, brand, sku,
            operation_mode=operation_mode,
            use_case=use_case,
            brand_prompt_text=brand_prompt_text,
            category_prompt_text=category_prompt_text,
            taxonomy=taxonomy,  selected_taxonomy=selected_taxonomy,title=query or title,
        )
        web_task = self.searxng._search(base_query)
        targeted_task = self.searxng._search(targeted_query_str)
        is_mpn_valid = mpn and str(mpn).strip().lower() != 'none' and not str(mpn).startswith('UNK-')
        search_id = mpn if is_mpn_valid else title
        image_task = self.searxng.search_images(f"{brand} {search_id}")
        web_results, targeted_results, image_results = await asyncio.gather(
            web_task, targeted_task, image_task, return_exceptions=True
        )
        if isinstance(web_results, Exception):
            logger.error(f"Web search failed: {web_results}")
            web_results = []
        if isinstance(targeted_results, Exception):
            logger.warning(f"Targeted search failed: {targeted_results}")
            targeted_results = []
        if isinstance(image_results, Exception):
            logger.warning(f"Image search failed: {image_results}")
            image_results = []
        if not web_results and not targeted_results:
            logger.warning(f"SearXNG returned no results for {mpn}")
            return [], []
        image_urls = list({img.get("img_src") for img in image_results if img.get("img_src")})
        seen_urls = set()
        merged = []
        for r in (targeted_results + web_results):
            url = r.get('url', '')
            if url and url not in seen_urls:
                seen_urls.add(url)
                merged.append(r)
        merged = [
            r for r in merged
            if not any(d in r.get('url', '').lower() for d in BLOCKED_DOMAINS)
        ]
        if not merged:
            logger.warning(f"All results blocked for {mpn}")
            return [], image_urls[:3]
        logger.info(f"After domain filter: {len(merged)} results for {mpn}")
        brand_lower = (brand or "").lower()
        mpn_lower = (mpn or "").lower()
        sku_lower = (sku or "").lower()
        is_mpn_valid = mpn and str(mpn).strip().lower() != 'none' and not str(mpn).startswith('UNK-')
        def is_relevant(r: dict) -> bool:
            text = (
                r.get('title', '') + ' ' +
                r.get('content', '') + ' ' +
                r.get('url', '')
            ).lower()
            
            if is_mpn_valid:
                # Strict: Must have brand and MPN
                return mpn_lower in text or brand_lower in text
            else:
                # Flexible: Brand must be present, check title keywords
                title_words = [w.lower() for w in (query or title).split() if len(w) > 3]
                title_matches = sum(1 for w in title_words if w in text)
                return brand_lower in text and title_matches >= 2
        relevant_results = [r for r in merged if is_relevant(r)]
        if relevant_results:
            web_results = relevant_results
            logger.info(f"Pre-filter: {len(web_results)} relevant results for {mpn}")
        else:
            logger.warning(f"Pre-filter: no relevant results for {mpn} — all off-topic")
            return [], image_urls[:3]
        web_text = "\n".join(
            f"[{i+1}] {r.get('title', 'No title')}\n    URL: {r['url']}\n    Description: {r.get('content', '')[:150]}"
            for i, r in enumerate(web_results[:15])
        )
        image_text = "\n".join(f"- {url}" for url in image_urls[:10])
        prompt = f"""
        PRODUCT:
        - Brand: {brand}
        - Identifier: {mpn if is_mpn_valid else title}
        WEB SEARCH RESULTS:
        {web_text}
        POSSIBLE PRODUCT IMAGES (from image search):
        {image_text or "None found"}
        TASK:
        Select up to {self.max_results} URLs most likely to contain technical specs, datasheets, or product data for THIS SPECIFIC PRODUCT.
        STRICT RULES:
        - ONLY select URLs from the list above — never invent URLs
        - ONLY select pages specifically about {brand} {mpn}
        - REJECT URLs from: social media, messaging apps, support sites, news sites, forums, Chinese platforms, tutorial sites, car dealerships, sports sites
        - REJECT generic homepages (e.g., dewalt.com/ with no product path)
        - ACCEPT specific product pages even if they are on manufacturer sites
        - A URL like dewalt.com/product/dcf403 IS acceptable
        - A URL like dewalt.com/ or dewalt.com/products IS NOT acceptable
        - PREFER: specific product pages with the MPN in the URL, manufacturer official product pages, industrial distributors, PDF datasheets
        - If fewer than {self.max_results} good URLs exist, return only the valid ones — do not pad with irrelevant URLs
        Return a JSON object with:
        - "selected_urls": list of chosen web URLs (empty list if none are relevant)
        - "candidate_image_urls": list of image URLs matching this product (max 3)
        """
        final_urls = []
        candidate_imgs = image_urls[:3]
        try:
            result = await call_llm_with_schema(
                prompt=prompt,
                response_model="SmartSearchResponse",
                llm_provider=self.llm_provider,
                estimated_tokens=500,
            )
            if result and result.selected_urls:
                valid_urls = {r["url"] for r in web_results}
                filtered = [u for u in result.selected_urls if u in valid_urls]
                hallucinated = len(result.selected_urls) - len(filtered)
                if hallucinated:
                    logger.warning(f"Removed {hallucinated} hallucinated URLs")
                candidate_imgs = result.candidate_image_urls if result.candidate_image_urls else []
                logger.info(f"Smart search for {mpn}: {filtered}")
                final_urls = filtered
        except Exception as e:
            logger.exception(f"LLM filtering failed: {e}")
            final_urls = [r["url"] for r in web_results[:self.max_results]]
        logger.info(f"BEFORE MANUFACTURER SCORING: final_urls={final_urls}, brand={brand}")
        if final_urls and brand:
            final_urls = await self.llm_score_manufacturer_urls(
                urls=final_urls,
                brand=brand,
                mpn=mpn,
                upc=sku 
            )
            logger.info(f"AFTER MANUFACTURER SCORING: {final_urls}")
        return final_urls, candidate_imgs
    async def llm_score_manufacturer_urls(
    self,
    urls: List[str],
    brand: str,
    mpn: str,
    upc: Optional[str] = None,
) -> List[str]:
        logger.info(f"llm_score_manufacturer_urls called with {len(urls)} URLs, brand={brand}, mpn={mpn}")
        from app.llm import call_llm_with_schema
        if not urls:
            return urls

        urls_list = "\n".join(f"- {url}" for url in urls)
        is_mpn_valid = mpn and str(mpn).strip().lower() != 'none' and not str(mpn).startswith('UNK-')

        prompt = f"""
        You are scoring candidate URLs to find the official manufacturer product page.

    Product:
    - Brand: {brand}
    - MPN: {mpn}
    - UPC: {upc if upc else 'Not provided'}

    Candidate URLs:
    {urls_list}
    
    Instructions:
    For each URL, assign a confidence score (0-100) based on:
    - +50 if the URL contains the exact brand name (case-insensitive)
    - +40 if the URL contains the exact MPN
    - +10 if the URL contains the UPC
    - Additional +10 if the domain is the official manufacturer domain (e.g., brand.com)
    - Subtract -20 if the domain is a known retailer (amazon, walmart, homedepot, grainger, zoro, etc.)

    Return JSON with:
    - "scored_urls": array of objects, each with "url", "score", "reasoning"
    - "best_url": the URL with the highest score (or null if none above 60)

    Return only valid JSON.
    """
        try:
            result = await call_llm_with_schema(
                prompt=prompt,
                response_model="ManufacturerScoringResponse",
                llm_provider=self.llm_provider,
                estimated_tokens=800
            )
            # Sort by score descending
            sorted_urls = [su.url for su in sorted(result.scored_urls, key=lambda x: x.score, reverse=True)]
            logger.info(f"Priority order: {sorted_urls}")
            return sorted_urls
        except Exception as e:
            logger.warning(f"LLM scoring failed: {e}, returning original order")
            return urls
