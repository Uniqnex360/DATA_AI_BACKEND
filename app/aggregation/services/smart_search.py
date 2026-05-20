import logging
from typing import List, Optional
from pydantic import BaseModel
from app.aggregation.interfaces import ISearchService
from app.rules.rule_engine import RuleEngine
from app.search.searxng_service import SearXNGSearchService
from sqlalchemy.ext.asyncio import AsyncSession
import asyncio
logger = logging.getLogger("smart_search")
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
class TargetedQueryResponse(BaseModel):
    search_query: str
class SmartSearchService(ISearchService):
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
            prompt = f"""
        You are a product data researcher. Given a product, generate the single best 
        Google search query to find its official specifications or datasheet page.
        Product:
        - Brand: {brand}
        - MPN: {mpn}
        Rules:
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
        return f"{brand} {mpn} specifications"
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
    ) -> tuple[List[str], List[str]]:
        from app.aggregation.aggregate_product import call_llm_with_schema
        if direct_urls:
            logger.info(f"Using {len(direct_urls)} direct URLs for {mpn}: {direct_urls}")
            image_urls = []
            image_task = self.searxng.search_images(f"{brand} {mpn}")
            image_results = await image_task
            if not isinstance(image_results, Exception):
                image_urls = list({img.get("img_src") for img in image_results if img.get("img_src")})
            return direct_urls, image_urls[:3]
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
            taxonomy=taxonomy,  selected_taxonomy=selected_taxonomy,
        )
        web_task = self.searxng._search(base_query)
        targeted_task = self.searxng._search(targeted_query_str)
        image_task = self.searxng.search_images(f"{brand} {mpn}")
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
        def is_relevant(r: dict) -> bool:
            text = (
                r.get('title', '') + ' ' +
                r.get('content', '') + ' ' +
                r.get('url', '')
            ).lower()
            return (
                mpn_lower in text or
                brand_lower in text or
                (sku_lower and len(sku_lower) > 4 and sku_lower in text)
            )
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
- MPN: {mpn}
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
        return final_urls, candidate_imgs
    async def _discover_manufacturer_website(
        self, 
        brand: str
    ) -> Optional[str]:
        """Use LLM to find the brand's official manufacturer website."""
        from app.aggregation.aggregate_product import call_llm_with_schema
        prompt = f"""
    Find the OFFICIAL manufacturer website for this brand.
    Brand: {brand}
    Rules:
    - Return the official manufacturer/corporate website, NOT a retailer
    - This is the company that MAKES the products under this brand
    - Examples:
    - "Craftsman" → "https://www.craftsman.com"
    - "Dewalt" → "https://www.dewalt.com"
    - "Ace" → "https://www.acehardware.com"
    - "Milwaukee" → "https://www.milwaukeetool.com"
    - "Steel Grip" → "" (if it's a private label with no official site)
    - If the brand is a store brand, private label, or has no dedicated website, return empty string
    - Do NOT return retailer sites like amazon.com, homedepot.com, etc.
    - Only return a URL if you are confident it's the manufacturer's official site
    Return JSON: {{"manufacturer_url": "url or empty", "confidence": 0.0-1.0}}
    """
        try:
            result = await call_llm_with_schema(
                prompt=prompt,
                response_model="ManufacturerWebsiteResponse",
                llm_provider=self.llm_provider,
                estimated_tokens=200
            )
            if result and result.manufacturer_url and result.confidence > 0.7:
                logger.info(f"LLM discovered manufacturer URL for {brand}: {result.manufacturer_url} (confidence: {result.confidence})")
                return result.manufacturer_url
            elif result:
                logger.info(f"LLM found URL for {brand} but confidence too low: {result.confidence}")
        except Exception as e:
            logger.warning(f"Failed to discover manufacturer URL for {brand}: {e}")
        return None
