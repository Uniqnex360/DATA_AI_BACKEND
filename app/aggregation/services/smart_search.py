import logging
from typing import Any, List, Optional, Dict
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
    reasoning: str = ""
class URLSelectionResponse(BaseModel):
    best_url: Optional[str] = Field(
        None, description="The most accurate product detail page URL found.")
    confidence: float = Field(
        0.0, description="Confidence score from 0.0 to 1.0")
    reasoning: str = Field(
        ..., description="Short explanation of why this URL was chosen over others.")
class IdentityVerificationResponse(BaseModel):
    is_match: bool = Field(
        ..., description="True if this page is definitely the Product Detail Page for the requested item.")
    confidence: float = Field(
        ..., description="How certain you are (0.0 to 1.0).")
    reasoning: str = Field(
        ..., description="Why this page matches or doesn't match.")
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
        "extra": "forbid",
        "json_schema_extra": {
            "additionalProperties": False
        }
    }
class ManufacturerUrlResponse(BaseModel):
    verified_urls: List[ManufacturerUrl] = Field(
        default_factory=list,
        description="List of verified manufacturer URLs"
    )
    model_config = {
        "extra": "forbid"
    }
class NavigationResponse(BaseModel):
    url: str
    exact_match: bool = False
    page_type: str
    confidence: float = 0.5
    reasoning: Optional[str] = None
class TargetedQueryResponse(BaseModel):
    search_query: str
class SmartSearchService(ISearchService):
    def is_likely_pdp_url(self, url: str) -> bool:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        url_lower = (parsed.scheme + "://" +
                     parsed.netloc + parsed.path).lower()
        # NEW
        import re

        reject_path_patterns = [
            '/lighting/', '/sale', '/january-sale',
            '/collections/', '/brands/',
            '/search', '/category',
            '?page=',
            '/stores/', '/store-locator/', '/find-a-store/',
            '/locations/', '/our-stores/',
            '/login', '/register', '/account',
            '/signin', '/signup',
            '/about', '/contact', '/faq', '/help',
            '/privacy', '/terms', '/cookies',
            '/blog/', '/news/', '/articles/',
            '/journal/', '/editorial/',
            'social-stories', 'social+stories',
            '/pages/',
            '/styles',
            '/compliance-',
            '/materials',
            '/reviews/', '/review/', '/ratings/', '/rating/',
            '/q-and-a/', '/questions-and-answers/', '/write-a-review',
        ]
        reject_word_patterns = [
            'forum', 'topic',  'community', 'answers',
            'how-to', 'wiki', 'recipe', 'brewing', 'download', 'article',
            'member', 'trophies',
        ]

        if any(p in url_lower for p in reject_path_patterns):
            logger.info(f"   Rejected PDP check (category/sale pattern): {url}")
            return False

        for w in reject_word_patterns:
            if re.search(rf'(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])', url_lower):
                logger.info(f"   Rejected PDP check (word match '{w}'): {url}")
                return False
        path_segments = [s for s in parsed.path.strip('/').split('/') if s]
        generic_keywords = ['lighting', 'lanterns', 'pendant', 'wall-lights', 'ceiling-lights',
                            'outdoor', 'indoor', 'task', 'sale', 'collection', 'category',
                            'january', 'february', 'march', 'april', 'may', 'june']
        for segment in path_segments:
            if segment in generic_keywords:
                logger.info(
                    f"   Rejected PDP check (generic keyword '{segment}'): {url}")
                return False
        if len(path_segments) < 1:
            return False
        logger.info(f"  ✓ Accepted PDP check: {url}")
        return True
    def __init__(
        self,
        llm_provider: str,
        db: AsyncSession,
        searxng_url: Optional[str] = None,
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
        leaf_node = ""
        if taxonomy:
            leaf_node = taxonomy.split('>')[-1].strip()
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
            is_mpn_valid = mpn and str(mpn).strip().lower(
            ) != 'none' and not str(mpn).startswith('UNK-')
            identifier = f"MPN: {mpn}" if is_mpn_valid else f"Product: {title}"
            prompt = f"""
        You are a product data researcher. Given a product, generate the single best 
        Google search query to find its official specifications or datasheet page.
        Product:
        - Brand: {brand}
        - Category: {leaf_node}
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
        return f"{brand} {mpn if is_mpn_valid else title} {leaf_node} specifications"
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
        direct_scored = None
        if direct_urls:
            direct_urls = [
                url for url in direct_urls if self.is_likely_pdp_url(url)]
            logger.info(
                f"Using {len(direct_urls)} direct URLs for {mpn}: {direct_urls}")
            if not direct_urls:
                logger.warning(
                    "All direct URLs were rejected as category/sale pages. Falling back to search.")
            image_urls = []        
            if direct_urls:
                is_mpn_valid = mpn and str(mpn).strip().lower(
                ) != 'none' and not str(mpn).startswith('UNK-')
                search_id = mpn if is_mpn_valid else title
                image_task = self.searxng.search_images(f"{brand} {search_id}")
                image_results = await image_task
                if not isinstance(image_results, Exception):
                    image_urls = list({img.get("img_src")
                                      for img in image_results if img.get("img_src")})
                if direct_urls and brand:
                    logger.info(
                        f"BEFORE MANUFACTURER SCORING (direct): {direct_urls}, brand={brand}")
                    scored_direct = await self.llm_score_manufacturer_urls(
                        urls=direct_urls,
                        brand=brand,
                        mpn=mpn,
                        upc=sku
                    )
                    logger.info(
                        f"AFTER MANUFACTURER SCORING (direct): {scored_direct}")
                    direct_scored = scored_direct
                else:
                    direct_scored = direct_urls
            else:
                logger.warning(
                    "All direct URLs rejected as category/sale pages. Falling back to search.")
        BLOCKED_DOMAINS = [
            'konghq.com',
            'miricanvas.com',
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
            taxonomy=taxonomy,  selected_taxonomy=selected_taxonomy, title=query or title,
        )
        is_mpn_valid = mpn and str(mpn).strip().lower(
        ) != 'none' and not str(mpn).startswith('UNK-')
        import re
        targeted_query_no_site = None
        if 'site:' in targeted_query_str:
            site_matches = re.findall(r'site:(\S+)', targeted_query_str)
            if site_matches:
                domains = [d.replace('www.', '') for d in site_matches]
                targeted_query_no_site = re.sub(
                    r'site:\S+\s*', '', targeted_query_str).strip()
                targeted_query_no_site = re.sub(
                    r'\bOR\b', '', targeted_query_no_site).strip()
                if is_mpn_valid and mpn.isdigit():
                    targeted_query_no_site = re.sub(
                        r'\b' + mpn + r'\b', '', targeted_query_no_site).strip()
                targeted_query_no_site = re.sub(
                    r'\s+', ' ', targeted_query_no_site)
                targeted_query_no_site = f"{targeted_query_no_site} {' '.join(domains)}"
                logger.info(
                    f"Targeted query (no site: fallback): {targeted_query_no_site}")
        has_site_restriction = 'site:' in targeted_query_str

        # NEW
        if has_site_restriction:
            site_matches = re.findall(r'site:(\S+)', targeted_query_str)
            domains = [d.replace('www.', '') for d in site_matches]
            query_no_site = re.sub(r'site:\S+\s*', '', targeted_query_str).strip()
            query_no_site = re.sub(r'\bOR\b', '', query_no_site).strip()
            query_no_site = re.sub(r'\s+', ' ', query_no_site)

            per_site_results = []
            for d in domains:
                try:
                    res = await self.searxng._search(f"site:{d} {query_no_site}")
                    res = [r for r in (res or []) if d in r.get('url', '').lower()]
                    has_pdp_candidate = any(self.is_likely_pdp_url(r.get('url', '')) for r in res)
                    if not res or not has_pdp_candidate:
                        simple_query = f"site:{d} {brand} {mpn}"
                        logger.info(f"Per-site '{d}' got {len(res)} results but no PDP candidate, retrying simple: {simple_query}")
                        await asyncio.sleep(0.3)
                        res_retry = await self.searxng._search(simple_query)
                        res_retry = [r for r in (res_retry or []) if d in r.get('url', '').lower()]
                        if res_retry:
                            res = res_retry
                    per_site_results.append(res)
                    logger.info(f"Per-site search '{d}': {len(res)} results (domain-filtered)")
                except Exception as e:
                    logger.warning(f"Per-site search failed for {d}: {e}")
                    per_site_results.append([])
                await asyncio.sleep(0.3)

            search_id = mpn if is_mpn_valid else title
            try:
                image_results = await self.searxng.search_images(f"{brand} {search_id}")
            except Exception as e:
                logger.warning(f"Image search failed: {e}")
                image_results = []

            targeted_results = []
            for site_res in per_site_results:
                targeted_results.extend(site_res)

            web_results = []
            logger.info(
                f"Taxonomy/category prompt restricts to specific sites — ran {len(domains)} per-site searches: {domains}"
            )
        else:
            web_task = self.searxng._search(base_query)
            targeted_task = self.searxng._search(targeted_query_str)
            search_id = mpn if is_mpn_valid else title
            image_task = self.searxng.search_images(f"{brand} {search_id}")
            web_results, targeted_results, image_results = await asyncio.gather(
                web_task, targeted_task, image_task, return_exceptions=True
            )
        if targeted_query_no_site and (isinstance(targeted_results, Exception) or not targeted_results or len(targeted_results) == 0):
            logger.info(
                f"Site: query failed, trying without site: {targeted_query_no_site}")
            targeted_results = await self.searxng._search(targeted_query_no_site)
            if targeted_results and not isinstance(targeted_results, Exception):
                logger.info(
                    f"Fallback (no site:) found {len(targeted_results)} results")
                fallback_domains = re.findall(r'site:(\S+)', targeted_query_str)
                fallback_domains = [d.replace('www.', '').replace('.co.uk', '') for d in fallback_domains]
                brand_lower = (brand or "").lower()
                targeted_results = [
                    r for r in targeted_results
                    if any(d in r.get('url', '') for d in fallback_domains) or brand_lower in r.get('url', '').lower()
                ]
                logger.info(
                    f"No-site results filtered: {len(targeted_results)} results")
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
        image_urls = list({img.get("img_src")
                          for img in image_results if img.get("img_src")})
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
        logger.info(
            f"[DEBUG] URLs after domain filter (before relevance check): {[r.get('url') for r in merged[:10]]}")
        if not merged:
            logger.warning(f"All results blocked for {mpn}")
            return [], image_urls[:3]
        logger.info(f"After domain filter: {len(merged)} results for {mpn}")
        brand_lower = (brand or "").lower()
        mpn_lower = (mpn or "").lower()
        sku_lower = (sku or "").lower()
        is_mpn_valid = mpn and str(mpn).strip().lower(
        ) != 'none' and not str(mpn).startswith('UNK-')
        def is_relevant(r: dict) -> bool:
            text = (
                r.get('title', '') + ' ' +
                r.get('content', '') + ' ' +
                r.get('url', '')
            ).lower()
            normalized_text = re.sub(r'[-\s.]', '', text)
            normalized_mpn = re.sub(r'[-\s.]', '', mpn_lower)
            if is_mpn_valid:
                if mpn.isdigit():
                    return mpn_lower in text and brand_lower in text
                return normalized_mpn in normalized_text or brand_lower in text
            else:
                title_words_old = [w.lower() for w in (
                    query or title).split() if len(w) > 3]
                title_matches_old = sum(
                    1 for w in title_words_old if w in text)
                if brand_lower in text and title_matches_old >= 2:
                    return True
            title_keywords = [w.lower()
                              for w in (title or "").split() if len(w) > 3]
            if title_keywords and brand_lower in text:
                matches = sum(1 for kw in title_keywords if kw in text)
                match_ratio = matches / len(title_keywords)
                if match_ratio >= 0.6:
                    logger.info(
                        f"✓ Rescued result via Name Density ({int(match_ratio*100)}%): {r.get('url')}")
                    return True
            return False
        relevant_results = []
        for r in merged:
            if is_relevant(r):
                relevant_results.append(r)
            elif targeted_query_no_site and brand_lower in (
                r.get('title', '') + ' ' +
                r.get('content', '') + ' ' + r.get('url', '')
            ).lower():
                relevant_results.append(r)
                logger.info(f"Passed for LLM verification: {r.get('url')}")
        if relevant_results:
            web_results = relevant_results
            logger.info(
                f"Pre-filter: {len(web_results)} relevant results for {mpn}")
        else:
            logger.warning(
                f"Pre-filter: no relevant results for {mpn} — all off-topic")
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
        - MANDATORY DOMAIN DIVERSITY: If results exist from more than one retailer domain in the WEB SEARCH RESULTS above, you MUST include at least one URL from each distinct domain in your selection (as long as it's a valid product page), even if one domain's match is stronger. Do not select multiple URLs from the same domain unless no other domain has a valid candidate.
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
                pdp_filtered = [u for u in filtered if self.is_likely_pdp_url(u)]
                non_pdp_removed = len(filtered) - len(pdp_filtered)
                if non_pdp_removed:
                    logger.info(f"Removed {non_pdp_removed} non-PDP URLs (reviews/ratings/etc.) after LLM selection")
                filtered = pdp_filtered
                candidate_imgs = result.candidate_image_urls if result.candidate_image_urls else []
                logger.info(f"Smart search for {mpn}: {filtered}")
                final_urls = filtered
        except Exception as e:
            logger.exception(f"LLM filtering failed: {e}")
            final_urls = [r["url"] for r in web_results[:self.max_results]]
        if final_urls and targeted_query_str and 'site:' in targeted_query_str:
            site_matches = re.findall(r'site:(\S+)', targeted_query_str)
            if site_matches:
                preferred_domains = [d.replace('www.', '') for d in site_matches]
                preferred_urls = [
                    r['url'] for r in web_results
                    if any(pd in r.get('url', '') for pd in preferred_domains)
                    and self.is_likely_pdp_url(r['url'])
                ]
                if preferred_urls:
                    final_urls = preferred_urls[:2] + \
                        [u for u in final_urls if u not in preferred_urls]
                    logger.info(
                        f"Boosted preferred domain URLs: {preferred_urls[:2]}")
        if direct_scored:
            final_urls = list(dict.fromkeys(direct_scored + final_urls))
            logger.info(f"Merged direct + broad URLs: {final_urls}")
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
        logger.info(
            f"llm_score_manufacturer_urls called with {len(urls)} URLs, brand={brand}, mpn={mpn}")
        from app.llm import call_llm_with_schema
        if not urls:
            return urls
        urls_list = "\n".join(f"- {url}" for url in urls)
        is_mpn_valid = mpn and str(mpn).strip().lower(
        ) != 'none' and not str(mpn).startswith('UNK-')
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
            sorted_urls = [su.url for su in sorted(
                result.scored_urls, key=lambda x: x.score, reverse=True)]
            logger.info(f"Priority order: {sorted_urls}")
            return sorted_urls
        except Exception as e:
            logger.warning(
                f"LLM scoring failed: {e}, returning original order")
            return urls
