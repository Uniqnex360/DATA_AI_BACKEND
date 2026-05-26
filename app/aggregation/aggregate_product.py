import re
from typing import Dict, List, Optional
from sqlalchemy import func
from app.core.config import settings
from app.llm import call_llm_with_schema
from typing import Dict, Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from app.aggregation.pipeline import AggregationPipeline
from sqlmodel import or_, select
from app.aggregation.prompts.enrichment_prompts import build_enrichment_prompt
from app.aggregation.prompts.extraction_prompts import build_extraction_prompt, build_pdf_extraction_prompt
from app.aggregation.prompts.validation_prompts import build_validation_prompt
from app.aggregation.services.search_service import SerpApiSearchService
from app.aggregation.services.download_service import HttpDownloadService
from app.aggregation.services.smart_search import SmartSearchService
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential
from urllib.parse import urlparse
from app.models.business_rule import BrandPrompt, CategoryPrompt
from app.models.category import Category
from app.models.enums import RuleStatus
from app.models.product_attribute_link import ProductAttributeValueLinkModel
from app.models.brand import Brand
from app.models.attribute import Attribute, AttributeValue
from app.aggregation.services.extraction_service import (
    ExtractionService, HtmlExtractor, PdfExtractor, PlaywrightExtractor)
from app.models.product import Product
from app.models.project import Project
import asyncio
from app.aggregation.services.image_service import extract_best_image, extract_best_image_fallback
import logging
from app.rules.rule_engine import RuleEngine
from app.services.product_discovery_service import ProductDiscoveryService
from app.utils.image_validator import validate_image_url
logger = logging.getLogger("aggregate_product")


def build_pipeline() -> AggregationPipeline:
    return AggregationPipeline(
        search_service=SerpApiSearchService(max_results=5),
        download_service=HttpDownloadService(timeout=30),
        extraction_service=ExtractionService(extractors=[
            HtmlExtractor(),
            PlaywrightExtractor(),
            PdfExtractor(),
        ]),
    )


def chunk_attributes(attributes: List[str], chunk_size: int = 10) -> List[List[str]]:
    return [attributes[i:i + chunk_size] for i in range(0, len(attributes), chunk_size)]


logger = logging.getLogger("aggregate_product")


async def extract_fallback_image(html: str, base_url: str) -> Optional[str]:
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin
    soup = BeautifulSoup(html, 'html.parser')
    candidates = []
    HARD_BLOCK_KEYWORDS = [
        'logo', 'icon', 'banner', 'button', 'spacer', 'placeholder', 'loading',
        'pixel', 'tracking', 'social', 'share', 'facebook', 'twitter', 'instagram',
        'og-image', 'social-share', 'carton', 'box', 'camozzi', 'default', 'nophoto'
    ]

    def is_junk(url_str: str) -> bool:
        u = url_str.lower()
        return any(k in u for k in HARD_BLOCK_KEYWORDS)
    meta = soup.find('meta', property='og:image')
    if meta and meta.get('content'):
        url = urljoin(base_url, meta['content'])
        if not is_junk(url):
            candidates.append((url, 100))
    meta = soup.find('meta', attrs={'name': 'twitter:image'})
    if meta and meta.get('content'):
        url = urljoin(base_url, meta['content'])
        if not is_junk(url):
            candidates.append((url, 90))
    for img in soup.find_all('img'):
        src = img.get('src')
        if not src:
            continue
        url = urljoin(base_url, src)
        if is_junk(url):
            continue
        alt = img.get('alt', '')
        score = 0
        if any(k in url.lower() for k in ['product', 'main', 'hero']):
            score += 20
        if alt and any(k in alt.lower() for k in ['product', 'main', 'hero']):
            score += 10
        try:
            width = img.get('width')
            height = img.get('height')
            if width and int(width) > 250:
                score += 15
            if height and int(height) > 250:
                score += 15
        except:
            pass
        candidates.append((url, score))
    candidates.sort(key=lambda x: x[1], reverse=True)
    for url, score in candidates:
        if score > 20:
            logger.info(f" Fallback selected image with score {score}: {url}")
            return url
    return None


def apply_unification(sources: List[Dict], groups: List) -> List[Dict]:
    mapping = {}
    for group in groups:
        canonical = group.canonical_name
        for old_name in group.grouped_attributes:
            mapping[old_name] = canonical
    unified_sources = []
    for source in sources:
        unified_attrs = []
        for attr in source['attributes']:
            if hasattr(attr, 'dict'):
                attr_dict = attr.dict()
            elif hasattr(attr, '__dict__'):
                attr_dict = attr.__dict__
            elif isinstance(attr, dict):
                attr_dict = attr.copy()
            else:
                attr_dict = {
                    'name': getattr(attr, 'name', str(attr)),
                    'value': getattr(attr, 'value', ''),
                    'unit': getattr(attr, 'unit', None),
                    'confidence': getattr(attr, 'confidence', 0.5)
                }
            if attr_dict['name'] in mapping:
                attr_dict['name'] = mapping[attr_dict['name']]
            unified_attrs.append(attr_dict)
        unified_sources.append({
            **source,
            'attributes': unified_attrs
        })
    return unified_sources


def extract_domains_and_generate_urls(prompt_text: str) -> List[str]:
    """Extract site: domains from prompt and generate search URLs."""
    if not prompt_text:
        return []
    import re
    domains = re.findall(
        r'site:([a-zA-Z0-9][-a-zA-Z0-9]*\.[a-zA-Z]{2,})', prompt_text)
    urls = []
    for domain in domains:
        urls.append(f"https://{domain}/")
    return urls


def extract_urls_from_prompt(prompt_text: str) -> List[str]:
    """Extract URLs from prompt text."""
    if not prompt_text:
        return []
    url_pattern = r'https?://[^\s\)\"]+'
    return re.findall(url_pattern, prompt_text)
# async def find_product_page_with_llm(
#     domain_url: str,
#     mpn: str,
#     brand: str,
#     title: str,
#     llm_provider: str,
#     upc: Optional[str] = None
# ) -> Optional[str]:
#     """
#     Use LLM to navigate a domain and find the product page.
#     Returns a single product URL (if found with confidence > 0.5)
#     and logs a content‑based score (0‑100) based on brand/MPN/UPC.
#     """
#     from app.llm import call_llm_with_schema
#     from pydantic import BaseModel
#     from app.aggregation.services.download_service import HttpDownloadService
#     try:
#         download_service = HttpDownloadService(timeout=30)
#         content = await download_service.download(domain_url)
#         if not content or content['type'] != 'html':
#             return None
#         html_text = content['raw_bytes'].decode('utf-8', errors='ignore')[:100000]
#         prompt = f"""
# You are a web navigation expert. Find the product page for this product on {domain_url}.
# Product:
# - MPN: {mpn}
# - Brand: {brand}
# - Name: {title}
# Homepage HTML (first 100k chars):
# {html_text}
# Task:
# 1. Look for search forms, navigation menus, category links
# 2. Find the most likely path to reach the product page
# 3. If there's a search function, determine the search URL pattern
# 4. Return the most promising product page URL or search URL
# Rules:
# - If you find an exact URL containing the MPN, return it immediately
# - If you find a category page (e.g., /hex-nuts), return that
# - If you find a search form, construct the search URL with MPN
# - Be specific - return full URLs
# Return JSON with:
# - product_url: the URL to try (full URL)
# - confidence: 0.0-1.0
# - reasoning: why you chose this URL
# """
#         result = await call_llm_with_schema(
#             prompt=prompt,
#             response_model="ProductPageResponse",
#             llm_provider=llm_provider,
#             estimated_tokens=4000
#         )
#         if not result or not result.product_url or result.confidence <= 0.5:
#             return None
#         candidate_url = result.product_url
#         logger.info(f"LLM found URL on {domain_url}: {candidate_url} (nav confidence={result.confidence:.2f})")
#         page_content = await download_service.download(candidate_url)
#         if page_content and page_content['type'] == 'html':
#             page_html = page_content['raw_bytes'].decode('utf-8', errors='ignore')[:200000]
#             upc_text = f"- UPC: {upc}" if upc else "- UPC: Not provided"
#             score_prompt = f"""
# You are evaluating a product page to see if it matches the given product.
# Product:
# - Brand: {brand}
# - MPN: {mpn}
# {upc_text}
# Page URL: {candidate_url}
# Page HTML (first 200k chars):
# {page_html}
# Task:
# 1. Determine if the brand name appears clearly on the page (text, meta, structured data).
# 2. Determine if the MPN or Brand appears clearly on the page OR in the URL.
# 3. Determine if the UPC appears (if provided).
# Then assign a score **exactly** following these rules:
# - If brand is found AND MPN is found → score = 90
# - If UPC is also found → add 10 → total 100
# - If only brand found OR only MPN found → score = 50
# - If neither found → score = 0
# Return JSON:
# {{
#     "brand_found": bool,
#     "mpn_found": bool,
#     "upc_found": bool,
#     "score": int,
#     "reasoning": "short explanation"
# }}
# """
#             score_result = await call_llm_with_schema(
#                 prompt=score_prompt,
#                 response_model="PageMatchScore",
#                 llm_provider=llm_provider,
#                 estimated_tokens=800
#             )
#             if score_result:
#                 logger.info(f"Content score for {candidate_url}: {score_result.score} – {score_result.reasoning}")
#             else:
#                 logger.warning(f"Scoring failed for {candidate_url}")
#         else:
#             logger.warning(f"Could not fetch candidate page for scoring: {candidate_url}")
#         return candidate_url
#     except Exception as e:
#         logger.warning(f"LLM navigation failed for {domain_url}: {e}")
#         return None


async def discover_manufacturer_urls(
    brand: str,
    taxonomy: str,
    mpn: str,
    llm_provider: str = 'openai'
) -> List[str]:
    """
    Discover official manufacturer URLs for a brand based on taxonomy/category.
    Returns list of verified manufacturer URLs (prioritizing product pages).
    """
    import httpx
    from urllib.parse import urlparse

    logger.info(f"Discovering manufacturer URLs for {brand} ({taxonomy})")

    # ─────────────────────────────────────────────────────────────────
    # HARDCODED MANUFACTURER DOMAINS (fast path, avoids search)
    # ─────────────────────────────────────────────────────────────────
    KNOWN_MANUFACTURERS = {
        "milton": [
            "https://miltonindustries.com/products/pistol-grease-gun-high-pressure-high-volume",
            "https://miltonindustries.com"
        ],
        "graco": ["https://graco.com"],
        "milwaukee": ["https://milwaukeetool.com"],
        "craftsman": ["https://craftsman.com"],
    }

    brand_lower = brand.lower()
    if brand_lower in KNOWN_MANUFACTURERS:
        urls = KNOWN_MANUFACTURERS[brand_lower]
        logger.info(f"Using known manufacturer URLs: {urls}")
        return urls

    try:
        from app.aggregation.services.download_service import HttpDownloadService
        download_service = HttpDownloadService(timeout=20,proxy=settings.PROXY_URL)

        # Step 1: Search for manufacturer website with taxonomy context
        taxonomy_parts = taxonomy.split(" > ")
        main_category = taxonomy_parts[-1] if taxonomy_parts else taxonomy

        search_queries = [
            f"{brand} {main_category} official website",
            f"{brand} {main_category} manufacturer",
            f"{brand} official website",
            f"{brand} manufacturer",
            f"{brand} brand website",
            brand,
        ]

        manufacturer_urls = []

        for query in search_queries:
            try:
                logger.info(f"Searching: {query}")

                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.get(
                        "http://searxng:8080/search",
                        params={
                            "q": query,
                            "format": "json",
                            "categories": "general",
                        }
                    )

                if response.status_code != 200:
                    continue

                results = response.json().get("results", [])

                if not results:
                    logger.debug(f"No results for: {query}")
                    continue

                logger.info(f"Found {len(results)} results")

                for result in results:
                    url = result.get("url", "")
                    title = result.get("title", "").lower()

                    if not url:
                        continue

                    # Skip non-manufacturer pages
                    blocked_keywords = [
                        '/search', '/results', '/compare', '/category',
                        'amazon', 'ebay', 'walmart', 'target', 'alibaba',
                        'aliexpress', 'pinterest', 'youtube', 'linkedin',
                        'twitter', 'facebook', 'instagram', 'reddit',
                        '.gov', '.edu', 'wikipedia'
                    ]

                    if any(keyword in url.lower() for keyword in blocked_keywords):
                        continue

                    domain = urlparse(url).netloc.lower()

                    # Must have brand in domain
                    if brand_lower not in domain:
                        continue

                    manufacturer_urls.append(url)
                    logger.info(f"✓ Found candidate: {url}")

                if manufacturer_urls:
                    break

            except Exception as e:
                logger.debug(f"Query failed: {e}")
                continue

        if not manufacturer_urls:
            logger.warning(f"No manufacturer URLs found for {brand}")
            return []

        # Step 2: Verify each URL and prioritize product pages
        verified_urls = []
        product_urls = []
        homepage_urls = []

        for url in manufacturer_urls:
            try:
                logger.info(f"Verifying manufacturer URL: {url}")
                content = await download_service.download(url)

                if content and content['type'] == 'html':
                    html = content['raw_bytes'].decode(
                        'utf-8', errors='ignore').lower()

                    has_brand = brand_lower in html
                    has_products = any(keyword in html for keyword in [
                        'product', 'catalog', 'shop', 'store', 'buy',
                        'category', 'collection', 'solutions', 'cart'
                    ])

                    if has_brand and has_products:
                        # Check if it's a product page (not homepage)
                        parsed = urlparse(url)
                        is_homepage = parsed.path in ['', '/']
                        is_product_page = '/product/' in url.lower() or '/products/' in url.lower()

                        if is_product_page:
                            product_urls.append(url)
                            logger.info(f"✓ VERIFIED product URL: {url}")
                        elif not is_homepage:
                            verified_urls.append(url)
                            logger.info(f"✓ VERIFIED manufacturer URL: {url}")
                        else:
                            homepage_urls.append(url)
                            logger.info(f"✓ VERIFIED homepage: {url}")

            except Exception as e:
                logger.debug(f"Verification failed: {e}")
                continue

        # ─────────────────────────────────────────────────────────────────
        # PRIORITIZE: Product URLs > Category URLs > Homepages
        # ─────────────────────────────────────────────────────────────────
        final_urls = []

        # First, try to find product URLs with the exact MPN
        for url in product_urls:
            if mpn.lower() in url.lower():
                final_urls.append(url)
                logger.info(f"Found product URL with MPN: {url}")

        # Then all product URLs
        if not final_urls:
            final_urls.extend(product_urls)

        # Then verified URLs (category pages, etc.)
        if not final_urls:
            final_urls.extend(verified_urls)

        # Finally homepages (only if nothing else found)
        if not final_urls:
            final_urls.extend(homepage_urls)

        # If still nothing, use LLM to rank
        if not final_urls and (product_urls or verified_urls):
            logger.info(
                f"Using LLM to rank {len(product_urls + verified_urls)} URLs")

            ranking_prompt = f"""
Identify the official manufacturer product page URL for {brand} {mpn}.

Product category: {main_category}
Product MPN: {mpn}

Candidate URLs:
{chr(10).join([f"- {url}" for url in (product_urls + verified_urls)[:5]])}

Return the single most relevant product page URL (not homepage).

Return JSON: {{"manufacturer_url": "https://...", "confidence": 0.0-1.0, "reasoning": "..."}}
"""
            llm_result = await call_llm_with_schema(
                prompt=ranking_prompt,
                response_model="ManufacturerWebsiteResponse",
                llm_provider=llm_provider,
                estimated_tokens=300
            )

            if llm_result and llm_result.manufacturer_url:
                final_urls = [llm_result.manufacturer_url]
                logger.info(f"LLM selected: {llm_result.manufacturer_url}")

        if final_urls:
            logger.info(
                f"Returning {len(final_urls)} manufacturer URLs: {final_urls}")
            return final_urls

        logger.warning(
            f"Could not find verified manufacturer URLs for {brand}")
        return []

    except Exception as e:
        logger.error(f"Manufacturer URL discovery failed: {e}")
        return []


async def find_product_page_with_llm(
    domain_url: str,
    mpn: str,
    brand: str,
    title: str,
    llm_provider: str,
    taxonomy: Optional[str] = None
) -> Optional[str]:
    """
    Search for product on specific domain and extract the actual product page URL.
    Uses smart parsing to find product detail pages from search results.
    Prioritizes product URLs over homepages.
    """
    import httpx
    from urllib.parse import urlparse, urljoin
    import re

    try:
        domain = urlparse(domain_url).netloc.replace('www.', '')

        # Try different search queries to maximize chances of finding the product
        search_queries = [
            f"{brand} {mpn}",  # Brand + MPN
            mpn,  # Just MPN
            f"{brand} {title.split()[0]}",  # Brand + first word of title
        ]
        if taxonomy:
            taxonomy_parts = taxonomy.split(" > ")
            main_category = taxonomy_parts[-1] if taxonomy_parts else taxonomy
            search_queries.insert(0, f"{brand} {main_category} {mpn}")
            search_queries.insert(1, f"{main_category} {mpn}")
            search_queries.insert(2, f"{brand} {main_category}")

        logger.info(f"Searching {domain} for {mpn}")

        from app.aggregation.services.download_service import HttpDownloadService
        download_service = HttpDownloadService(timeout=20,proxy=settings.PROXY_URL)

        for query in search_queries:
            try:
                logger.info(f"Query: {query}")

                # Use SearXNG without site: restriction first
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.get(
                        "http://searxng:8080/search",
                        params={
                            "q": query,
                            "format": "json",
                            "categories": "general",
                        }
                    )

                if response.status_code != 200:
                    continue

                results = response.json().get("results", [])

                if not results:
                    logger.debug(f"No results for: {query}")
                    continue

                logger.info(f"Found {len(results)} general results")

                # Filter to domain and find product pages
                product_urls = []

                for result in results:
                    url = result.get("url", "")
                    result_title = result.get("title", "").lower()

                    # Must be from the target domain
                    if domain not in url:
                        continue

                    # Skip non-product pages
                    blocked_paths = ['/search', '/results', '/browse', '/category',
                                     '/categories', '/brand', '/brands', '/blog',
                                     '/news', '/about', '/register', '/login',
                                     '/cart', '/checkout', '/compare']

                    if any(blocked in url.lower() for blocked in blocked_paths):
                        continue

                    url_lower = url.lower()

                    # ★ CRITICAL: URL must contain brand OR title OR mpn ★
                    has_brand = brand.lower() in url_lower
                    has_title = title.lower() in url_lower or any(
                        word in url_lower for word in title.lower().split() if len(word) > 3
                    )
                    has_mpn = mpn.lower() in url_lower

                    if not (has_brand or has_title or has_mpn):
                        continue

                    product_urls.append(url)

                if not product_urls:
                    logger.debug(f"No product URLs for query: {query}")
                    continue

                logger.info(
                    f"Found {len(product_urls)} candidate product URLs")

                # ─────────────────────────────────────────────────────────
                # SORT URLs by priority: product pages first, homepages last
                # ─────────────────────────────────────────────────────────
                priority_urls = []
                homepage_urls = []
                category_urls = []

                for url in product_urls:
                    url_lower = url.lower()
                    parsed = urlparse(url)

                    # Product URLs (contain /product/ or /products/)
                    if '/product/' in url_lower or '/products/' in url_lower:
                        priority_urls.append(url)
                    # Homepage (domain only or just /)
                    elif parsed.path in ['', '/']:
                        homepage_urls.append(url)
                    # Category pages (contain /category/ or /collections/)
                    elif '/category/' in url_lower or '/collection/' in url_lower:
                        category_urls.append(url)
                    else:
                        # Other pages go to priority (might still be product)
                        priority_urls.append(url)

                # Combine: priority URLs first, then category, then homepages
                sorted_urls = priority_urls + category_urls + homepage_urls
                logger.info(
                    f"Sorted URLs: {len(priority_urls)} priority, {len(category_urls)} category, {len(homepage_urls)} homepage")

                # Verify each URL by downloading and checking content
                for url in sorted_urls:
                    try:
                        logger.info(f"Verifying: {url}")
                        content = await download_service.download(url)

                        if content and content['type'] == 'html':
                            html = content['raw_bytes'].decode(
                                'utf-8', errors='ignore').lower()

                            # Check for product info
                            has_mpn_in_html = mpn.lower() in html
                            has_brand_in_html = brand.lower() in html

                            # Check for e-commerce indicators
                            product_indicators = [
                                'add to cart', 'add to bag', 'buy now', 'add to quote',
                                'price', 'pricing', 'in stock', 'out of stock',
                                'product details', 'specifications', 'description',
                                'quantity', 'sku', '$', '€', '£', 'mpn', 'model'
                            ]

                            indicator_count = sum(
                                1 for indicator in product_indicators
                                if indicator in html
                            )

                            logger.info(
                                f"Content check: MPN={has_mpn_in_html}, "
                                f"Brand={has_brand_in_html}, "
                                f"Indicators={indicator_count}"
                            )

                            parsed_url = urlparse(url)
                            is_homepage = parsed_url.path in ['', '/']
                            is_product_url = '/product/' in url.lower() or '/products/' in url.lower()

                            # ─────────────────────────────────────────────
                            # ACCEPTANCE CRITERIA based on URL type
                            # ─────────────────────────────────────────────

                            # Product URLs (highest priority) - accept with brand + indicator
                            if is_product_url and has_brand_in_html and indicator_count >= 1:
                                logger.info(f"✓ VERIFIED product page: {url}")
                                return url

                            # Product URLs with MPN - accept immediately
                            if is_product_url and has_mpn_in_html:
                                logger.info(
                                    f"✓ VERIFIED product page (MPN found): {url}")
                                return url

                            # Regular pages (non-homepage) - need MPN or brand + indicators
                            if not is_homepage and (has_mpn_in_html or (has_brand_in_html and indicator_count >= 2)):
                                logger.info(f"✓ VERIFIED product page: {url}")
                                return url

                            # Homepage - ONLY accept if MPN is present (rare, but possible)
                            if is_homepage and has_mpn_in_html and has_brand_in_html and indicator_count >= 3:
                                logger.info(
                                    f"✓ VERIFIED homepage with product info: {url}")
                                return url

                    except Exception as e:
                        logger.debug(f"Verification failed: {e}")
                        continue

                # If no verification passed, return first priority URL (not homepage)
                if priority_urls:
                    logger.info(
                        f"✓ Using first priority URL (unverified): {priority_urls[0]}")
                    return priority_urls[0]
                elif product_urls:
                    logger.info(
                        f"✓ Using first matching URL (unverified): {product_urls[0]}")
                    return product_urls[0]

            except Exception as e:
                logger.debug(f"Query failed: {e}")
                continue

        # ★ FALLBACK: Direct domain-specific search for known retailers ★
        logger.info(
            f"SearXNG search failed, trying domain-specific fallback for {domain}")

        domain_fallbacks = {
            'zoro.com': {
                'search_url': f"https://www.zoro.com/search?q={mpn}",
                'product_pattern': r'href="([^"]*zoro\.com[^"]*)" [^>]*title="[^"]*{brand}[^"]*"',
            },
            'grainger.com': {
                'search_url': f"https://www.grainger.com/search?searchQuery={mpn}",
                'product_pattern': r'href="([^"]*grainger\.com/product[^"]*)"',
            },
            'surpluscenter.com': {
                'search_url': f"https://www.surpluscenter.com/?search={mpn}",
                'product_pattern': r'href="([^"]*surpluscenter\.com[^"]*)"',
            },
        }

        if domain in domain_fallbacks:
            fallback = domain_fallbacks[domain]
            try:
                logger.info(
                    f"Trying domain fallback: {fallback['search_url']}")
                content = await download_service.download(fallback['search_url'])

                if content and content['type'] == 'html':
                    html = content['raw_bytes'].decode(
                        'utf-8', errors='ignore')
                    html_lower = html.lower()

                    # Find all product links
                    all_links = re.findall(r'href="([^"]*)"', html)

                    # Filter to likely product pages
                    product_candidates = []
                    for link in all_links:
                        link_lower = link.lower()

                        # Skip non-product patterns
                        if any(x in link_lower for x in ['/search', '/category', '/brand', '/compare']):
                            continue

                        # Must have MPN or brand
                        if mpn.lower() in link_lower or brand.lower() in link_lower:
                            product_candidates.append(link)

                    logger.info(
                        f"Found {len(product_candidates)} product candidates")

                    # Priority: product URLs first
                    product_candidates.sort(
                        key=lambda x: 0 if '/product/' in x.lower() else 1)

                    # Verify each candidate
                    for candidate_url in product_candidates:
                        if not candidate_url.startswith('http'):
                            candidate_url = urljoin(domain_url, candidate_url)

                        try:
                            logger.info(
                                f"Verifying fallback URL: {candidate_url}")
                            candidate_content = await download_service.download(candidate_url)

                            if candidate_content and candidate_content['type'] == 'html':
                                candidate_html = candidate_content['raw_bytes'].decode(
                                    'utf-8', errors='ignore').lower()

                                has_mpn = mpn.lower() in candidate_html
                                has_brand = brand.lower() in candidate_html
                                is_product_url = '/product/' in candidate_url.lower() or '/products/' in candidate_url.lower()

                                product_indicators = [
                                    'add to cart', 'price', 'specifications', 'description']
                                indicator_count = sum(
                                    1 for ind in product_indicators if ind in candidate_html)

                                # Product URLs get lower threshold
                                if is_product_url and (has_mpn or has_brand):
                                    logger.info(
                                        f"✓ VERIFIED fallback product page: {candidate_url}")
                                    return candidate_url

                                if (has_mpn or has_brand) and indicator_count >= 1:
                                    logger.info(
                                        f"✓ VERIFIED fallback product page: {candidate_url}")
                                    return candidate_url
                        except Exception as e:
                            logger.debug(f"Fallback verification failed: {e}")
                            continue

                    # Return first candidate if no verification passed
                    if product_candidates:
                        final_url = product_candidates[0]
                        if not final_url.startswith('http'):
                            final_url = urljoin(domain_url, final_url)
                        logger.info(
                            f"✓ Using fallback URL (unverified): {final_url}")
                        return final_url

            except Exception as e:
                logger.debug(f"Domain fallback search failed: {e}")

        logger.warning(f"Could not find product page on {domain} for {mpn}")
        return None

    except Exception as e:
        logger.warning(f"Product discovery failed: {e}")
        return None


async def aggregate_product(
    mpn: str,
    title: str,
    sku: Optional[str] = None,
    upc: Optional[str] = None,
    brand: Optional[str] = None,
    taxonomy: Optional[str] = None,
    primary_attributes: Optional[List[str]] = None,
    db: Optional[AsyncSession] = None,
    project_id: str = None,
    llm_provider: str = 'openai',
    attribute_chunk: Optional[List[str]] = None,
    existing_excel_attrs: Optional[Dict[str, str]] = None,
    missing_llm_provider: str = None
) -> Dict:
    try:
        if missing_llm_provider is None:
            missing_llm_provider = llm_provider
        logger.info(f"Starting aggregation for {mpn}")
        logger.info("Stage 1: URL Discovery")

        brand_prompt_text = None
        if brand and db:
            brand_stmt = select(BrandPrompt.prompt_text).join(
                Brand, BrandPrompt.brand_id == Brand.id
            ).where(
                func.lower(Brand.name) == func.lower(brand),
                BrandPrompt.status == RuleStatus.ACTIVE
            )
            brand_result = await db.execute(brand_stmt)
            brand_row = brand_result.first()
            if brand_row:
                brand_prompt_text = brand_row[0]
        category_prompt_text = None
        selected_taxonomy = None
        matched_category_id = None
        if taxonomy and db:
            clean_taxonomy = taxonomy.strip()
            stmt = select(
                CategoryPrompt.prompt_text,
                CategoryPrompt.selected_taxonomy
            ).where(
                CategoryPrompt.status == RuleStatus.ACTIVE,
                CategoryPrompt.selected_taxonomy.isnot(None),
            )
            result = await db.execute(stmt)
            matching = [
                (len(sel_tax), prompt_text, sel_tax)
                for prompt_text, sel_tax in result.all()
                if clean_taxonomy.startswith(sel_tax)
            ]
            if matching:
                matching.sort(key=lambda x: x[0], reverse=True)
                category_prompt_text = matching[0][1]
                selected_taxonomy = matching[0][2]
                logger.info(
                    f"✓ Taxonomy prompt matched: '{selected_taxonomy}'")
            if not category_prompt_text:
                tax_parts = clean_taxonomy.split(" > ")
                candidate_paths = [
                    " > ".join(tax_parts[:i]) for i in range(len(tax_parts), 0, -1)
                ]
                last_segments = [p.strip() for p in tax_parts]
                cat_stmt = select(Category).where(
                    or_(
                        Category.full_path.in_(candidate_paths),
                        Category.name.in_(last_segments)
                    )
                )
                cat_result = await db.execute(cat_stmt)
                categories = sorted(
                    cat_result.scalars().all(),
                    key=lambda c: len(c.full_path or ""),
                    reverse=True
                )
                for category in categories:
                    prompt_stmt = select(CategoryPrompt).where(
                        CategoryPrompt.category_id == category.id,
                        CategoryPrompt.status == RuleStatus.ACTIVE,
                        CategoryPrompt.selected_taxonomy.is_(None)
                    ).limit(1)
                    prompt_result = await db.execute(prompt_stmt)
                    cat_prompt = prompt_result.scalars().first()
                    if cat_prompt:
                        category_prompt_text = cat_prompt.prompt_text
                        matched_category_id = category.id
                        logger.info(
                            f"✓ Category prompt matched: '{category.name}'")
                        break
            if not category_prompt_text:
                logger.info(
                    f"✗ No prompt found for taxonomy: '{clean_taxonomy}'")
        search_service = SmartSearchService(llm_provider, db=db, max_results=5)
        query = title if (
            mpn in title and brand in title) else f"{brand} {mpn} {title}"
        query = query.strip()

# =====================================================
# STAGE 1: URL DISCOVERY (Production Version)
# =====================================================
        direct_domains = set()
        direct_urls = []

        discovery_service = ProductDiscoveryService(max_results=5)

        # 1. Manufacturer domain
        if brand:
            manufacturer_domain = await discovery_service.discover_manufacturer_domain(
                brand=brand,
                category=taxonomy,
                db=db 
            )
            if manufacturer_domain:
                logger.info(f"✓ Manufacturer domain found: {manufacturer_domain}")
                direct_domains.add(urlparse(manufacturer_domain).netloc)

        # 2. Prompt-based domains (from BrandPrompt / CategoryPrompt)
        if brand_prompt_text:
            brand_urls = extract_urls_from_prompt(brand_prompt_text)
            if not brand_urls:
                brand_urls = extract_domains_and_generate_urls(brand_prompt_text)
            for url in brand_urls:
                direct_domains.add(urlparse(url).netloc)

        elif category_prompt_text:
            category_urls = extract_urls_from_prompt(category_prompt_text)
            if not category_urls:
                category_urls = extract_domains_and_generate_urls(category_prompt_text)
            for url in category_urls:
                direct_domains.add(urlparse(url).netloc)

        # 3. Convert domains → Product Pages using SerpApi
        for domain in direct_domains:
            logger.info(f"Searching product page on domain: {domain}")

            product_url = await discovery_service.find_product_page(
                domain=f"https://{domain}",
                brand=brand,
                mpn=mpn,
                title=title
            )

            if product_url:
                logger.info(f"✓ Found product page on {domain}: {product_url}")
                direct_urls.append(product_url)

        seen = set()
        direct_urls = [u for u in direct_urls if not (u in seen or seen.add(u))]
        

        if direct_urls:
            logger.info(f"Final direct product URLs for {mpn}: {direct_urls}")
        urls, candidate_images = await search_service.get_urls(
            query, mpn=mpn, brand=brand, sku=sku,
            brand_prompt_text=brand_prompt_text,
            category_prompt_text=category_prompt_text,
            taxonomy=taxonomy, direct_urls=direct_urls, selected_taxonomy=selected_taxonomy
        )
        if not urls:
            return {
                'status': 'failed',
                'reason': 'No sources found',
                'golden_record': {'attributes': {}}
            }
        logger.info(f"Stage 2: Download & Extraction from {len(urls)} sources")
        download_service = HttpDownloadService(
            timeout=30,proxy=settings.PROXY_URL
        )
        all_extractions = []
        _url_semaphore = asyncio.Semaphore(1)

        async def process_url(url):
            short_description = None
            long_description = None 
            async with _url_semaphore:
                try:
                    content = await download_service.download(url)
                    if content is None:
                        return None
                    if content['type'] == 'html':
                        html_text = content['raw_bytes'].decode(
                            'utf-8', errors='ignore')
                        logger.info(
                            f"Downloaded HTML from {url} - size: {len(html_text)} bytes")
                        attrs_to_use = primary_attributes or []
                        if attribute_chunk:
                            other_attrs = [
                                a for a in attrs_to_use if a not in attribute_chunk]
                            attrs_to_use = attribute_chunk + other_attrs
                        prompt_config = build_extraction_prompt(
                            product_name=title,
                            mpn=mpn,
                            brand=brand or "",
                            taxonomy=taxonomy or "",
                            primary_attributes=attrs_to_use,
                            html_content=html_text,
                            candidate_images=candidate_images, source_url=url
                        )
                        extraction_result = await call_llm_with_schema(
                            prompt=prompt_config['prompt'],
                            response_model="ExtractionResponse",
                            llm_provider=llm_provider,
                            estimated_tokens=3000
                        )
                        logger.info(f"=== EXTRACTION RESULTS FROM {url} ===")
                        if extraction_result and extraction_result.product_detected:
                            logger.info(f"Product detected: YES")
                            logger.info(
                                f"Image URL: {extraction_result.image_url if hasattr(extraction_result, 'image_url') else 'None'}")
                            logger.info(
                                f"Number of attributes extracted: {len(extraction_result.attributes)}")
                            for attr in extraction_result.attributes:
                                logger.info(
                                    f"  - {attr.name}: {attr.value} {getattr(attr, 'unit', '')}")
                        else:
                            logger.info(f"Product detected: NO")
                            logger.info(
                                f"Extraction result: {extraction_result}")
                        logger.info(f"=====================================")
                        attr_dicts = []
                        image_url = None
                        if extraction_result and extraction_result.product_detected:
                            if hasattr(extraction_result, 'image_url'):
                                image_url = extraction_result.image_url
                            if hasattr(extraction_result,'short_description'):
                                    short_description=extraction_result.short_description
                            if hasattr(extraction_result,'long_description'):
                                long_description=extraction_result.long_description
                            for attr in extraction_result.attributes:
                                attr_dicts.append({
                                    'name': attr.name,
                                    'value': attr.value,
                                    'unit': attr.unit if hasattr(attr, 'unit') else None,
                                    'confidence': attr.confidence if hasattr(attr, 'confidence') else 0.9
                                })
                            
                        if not image_url:
                            image_url = await extract_best_image(html_text, url, mpn)
                            if image_url:
                                logger.info(
                                    f"Fallback extracted image: {image_url}")
                        domain = urlparse(url).netloc
                        return {
                            'url': url,
                            'domain': domain,
                            'attributes': attr_dicts,
                            'image_url': image_url,
                            'source_type': 'html',
                            'short_description': short_description,  
                            'long_description': long_description  
                        }
                    elif content['type'] == 'pdf':
                        from app.aggregation.services.pdf_service import PDFExtractionService
                        pdf_service = PDFExtractionService(max_pages=10)
                        pdf_text = await pdf_service.extract_text(content['raw_bytes'])
                        if pdf_text and len(pdf_text.strip()) > 100:
                            logger.info(
                                f"Extracted {len(pdf_text)} chars from PDF")
                            attrs_to_use = primary_attributes or []
                            if attribute_chunk:
                                other_attrs = [
                                    a for a in attrs_to_use if a not in attribute_chunk]
                                attrs_to_use = attribute_chunk + other_attrs
                            prompt_config = build_pdf_extraction_prompt(
                                product_name=title,
                                mpn=mpn,
                                brand=brand or "",
                                taxonomy=taxonomy or "",
                                primary_attributes=attrs_to_use,
                                pdf_text=pdf_text
                            )
                            extraction_result = await call_llm_with_schema(
                                prompt=prompt_config['prompt'],
                                response_model="ExtractionResponse",
                                llm_provider=llm_provider,
                                estimated_tokens=4000
                            )
                            if extraction_result and extraction_result.product_detected:
                                attr_dicts = []
                                image_url = None
                                if hasattr(extraction_result, 'image_url'):
                                    image_url = extraction_result.image_url
                                
                                for attr in extraction_result.attributes:
                                    attr_dicts.append({
                                        'name': attr.name,
                                        'value': attr.value,
                                        'unit': getattr(attr, 'unit', None),
                                        'confidence': getattr(attr, 'confidence', 0.95)
                                    })
                                domain = urlparse(url).netloc
                                return {
                                    'url': url,
                                    'domain': domain,
                                    'attributes': attr_dicts,
                                    'image_url': image_url,
                                    'source_type': 'pdf'
                                }
                    return None
                except Exception as e:
                    logger.warning(f"Extraction failed for {url}: {e}")
                    return None
        tasks = [process_url(url) for url in urls[:5]]
        results = await asyncio.gather(*tasks)
        all_extractions = [r for r in results if r is not None]
        if not all_extractions:
            return {
                'status': 'failed',
                'reason': 'No valid extractions',
                'golden_record': {'attributes': {}}
            }
        logger.info(
            f"Stage 2 extracted {sum(len(s['attributes']) for s in all_extractions)} total attributes")
        logger.info("Stage 3: Combined Cleaning, Unification & Standardization")
        raw_attrs_for_combine = []
        for src_idx, source in enumerate(all_extractions):
            for attr in source['attributes']:
                raw_attrs_for_combine.append({
                    'temp_id': f"{src_idx}_{len(raw_attrs_for_combine)}",
                    'name': attr['name'],
                    'value': attr['value'],
                    'unit': attr.get('unit'),
                    'source_url': source['url'],
                    'confidence': attr.get('confidence', 0.9)
                })
        project = await db.get(Project, project_id) if db and project_id else None
        use_case = project.use_case.lower() if project and project.use_case else ""
        combine_prompt = _build_combined_prompt(
            raw_attrs_for_combine, brand, mpn, title, taxonomy, existing_excel_attrs=existing_excel_attrs, use_case=use_case)
        async for attempt in AsyncRetrying(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10)):
            with attempt:
                combined_result = await call_llm_with_schema(
                    prompt=combine_prompt,
                    response_model="UnifiedStandardizedResponse",
                    llm_provider=llm_provider,
                    estimated_tokens=3000 + len(raw_attrs_for_combine) * 200,
                    max_tokens=4000
                )
        golden_attributes = combined_result.attributes
        valid_source_urls = {source['url'] for source in all_extractions}
        for attr in golden_attributes:
            if hasattr(attr, 'sources') and attr.sources:
                attr.sources = [
                    src for src in attr.sources if src in valid_source_urls]
        logger.info(
            f"Stage 3 produced {len(golden_attributes)} unified attributes")
        validation_conflicts = {}
        excel_overrides = {}
        if "back filling" in use_case or "validation" in use_case:
            logger.info("Stage 4: Excel Validation")
            stmt = select(Product).where(Product.product_code == mpn)
            result = await db.execute(stmt)
            product = result.scalars().first()
            excel_attrs = {}
            if product:
                try:
                    val_stmt = (select(Attribute.attribute_name, AttributeValue.value).join(AttributeValue, AttributeValue.attribute_id == Attribute.id).join(
                        ProductAttributeValueLinkModel, ProductAttributeValueLinkModel.attribute_value_id == AttributeValue.id).where(ProductAttributeValueLinkModel.product_id == product.id))
                    val_result = await db.execute(val_stmt)
                    for attr_name, attr_value in val_result.all():
                        excel_attrs[attr_name] = attr_value
                except Exception as e:
                    logger.warning(
                        f"Failed to read Excel attributes from normalized tables: {e}")
            web_attrs = {attr.name: attr.value for attr in golden_attributes}
            validation_config = build_validation_prompt(
                excel_attributes=excel_attrs,
                web_attributes=web_attrs,
                mpn=mpn,
                taxonomy=taxonomy or ""
            )
            validation_result = await call_llm_with_schema(
                prompt=validation_config['prompt'],
                response_model="ValidationResponse",
                llm_provider=missing_llm_provider,
                estimated_tokens=1500
            )
            if "back filling" in use_case and "validation" not in use_case:
                for val in validation_result.validations:
                    if not val.matches and val.recommendation == "use_web":
                        excel_overrides[val.attribute_name] = val.web_value
                        validation_conflicts[val.attribute_name] = val.web_value
                        for attr in golden_attributes:
                            if attr.name == val.attribute_name:
                                attr.value = val.web_value
            elif "validation" in use_case and "back filling" not in use_case:
                for val in validation_result.validations:
                    if not val.matches and val.recommendation == "use_web":
                        validation_conflicts[val.attribute_name] = val.web_value
            elif "back filling" in use_case and "validation" in use_case:
                for val in validation_result.validations:
                    if not val.matches and val.recommendation == "use_web":
                        excel_overrides[val.attribute_name] = val.web_value
                        validation_conflicts[val.attribute_name] = val.web_value
                        for attr in golden_attributes:
                            if attr.name == val.attribute_name:
                                attr.value = val.web_value
            else:
                for val in validation_result.validations:
                    if not val.matches and val.recommendation == "use_web":
                        validation_conflicts[val.attribute_name] = val.web_value
        logger.info("Stage 5: Multi-source Aggregation")
        if golden_attributes:
            avg_conf = sum(a.confidence for a in golden_attributes) / \
                len(golden_attributes)
        else:
            avg_conf = 0.0
        golden_attr_dicts = [
            {
                'name': a.name,
                'value': a.value,
                'unit': a.unit,
                'confidence': a.confidence,
                'sources': a.sources
            }
            for a in golden_attributes
        ]
        best_short_description=None
        best_long_description=None
        for source in all_extractions:
            if source.get('short_description') and not best_short_description:
                best_short_description=source['short_description']
            if source.get('long_description') and not best_long_description:
                best_long_description=source['long_description']
            if best_short_description and best_long_description:
                break
        logger.info("Stage 6: Marketing Enrichment")
        enrichment_config = build_enrichment_prompt(
            golden_attributes=golden_attr_dicts,
            product_name=title,
            brand=brand or "",
            taxonomy=taxonomy or "",
            existing_short_description=best_short_description,
            existing_long_description=best_long_description
        )
        enrichment_result = await call_llm_with_schema(
            prompt=enrichment_config['prompt'],
            response_model="EnrichmentResponse",
            llm_provider=missing_llm_provider,
            estimated_tokens=2000,
            max_tokens=4000
        )
        best_image = extract_best_image_fallback(all_extractions)
        if not best_image and candidate_images:
            for candidate in candidate_images:
                is_valid = await validate_image_url(candidate)
                if is_valid:
                    logger.info(f"Fallback to SearXNG image: {candidate}")
                    best_image = candidate
                    break
        return {
            'status': 'success',
            'golden_record': {
                'attributes': {attr['name']: attr for attr in golden_attr_dicts},
                'short_description': enrichment_result.short_description or "",
                'long_description': enrichment_result.long_description,
                'features': enrichment_result.features,
                'sources_consulted': list({s['url'] for s in all_extractions}),
                'confidence': avg_conf
            },
            'validation_conflicts': validation_conflicts,
            'excel_overrides': excel_overrides,
            'image_url': best_image,
            'mode': 'backfill' if 'back filling' in use_case else 'standard'
        }
    except Exception as e:
        logger.error(f"Pipeline failed for {mpn}: {e}", exc_info=True)
        return {
            'status': 'failed',
            'reason': str(e),
            'golden_record': {'attributes': {}}
        }


def _build_combined_prompt(
    raw_attrs: List[Dict],
    brand: str,
    mpn: str,
    title: str,
    taxonomy: str,
    existing_excel_attrs: Optional[Dict[str, str]] = None,
    use_case: str = None
) -> str:
    attr_lines = []
    for a in raw_attrs:
        line = f"ID: {a['temp_id']}\n  Name: {a['name']}\n  Value: {a['value']}"
        if a.get('unit'):
            line += f"\n  Unit: {a['unit']}"
        line += f"\n  Confidence: {a.get('confidence', 0.9)}"
        if a.get('source_url'):
            line += f"\n  Source: {a['source_url']}"
        attr_lines.append(line)
    attributes_text = "\n\n".join(attr_lines)
    excel_section = ''
    if existing_excel_attrs and any(v.get('value') for v in existing_excel_attrs.values()):
        excel_lines = []
        for name, val in existing_excel_attrs.items():
            v = val.get('value', '')
            u = val.get('uom', '')
            if v:
                excel_lines.append(f"  {name}: {v}{' ' + u if u else ''}")
        if excel_lines:
            excel_section = f"""
        ═══════════════════════════════════════════════════════
        EXISTING EXCEL DATA  (original values from customer file)
        ═══════════════════════════════════════════════════════
        {chr(10).join(excel_lines)}
        These are the values already in the customer's system.
        Use these as reference when unifying — if a web source
        confirms the same value, boost confidence.
        If web sources contradict Excel, keep the web value
        but note the conflict in original_values.
        """
    validation_section = ""
    if use_case and 'validation' in use_case.lower():
        validation_section = """
    VALIDATION INSTRUCTIONS:
    For each final attribute, compare your cleaned web value
    against the Excel value above.
    If they differ, set conflict=true and include both values
    in original_values so the router can write the conflict
    to validation columns.
    """
    prompt = f"""
You are a Senior Product Data Engineer. Process the raw product attributes below.
Your job: clean → unify synonyms → standardize → return ONE canonical attribute per concept.
PRODUCT CONTEXT:
  MPN: {mpn}
  Brand: {brand}
  Title: {title}
  Taxonomy: {taxonomy or 'General'}
  ═══════════════════════════════════════════════════════
CONFLICT RESOLUTION RULES
═══════════════════════════════════════════════════════
When same attribute appears multiple times with different values:
1. Pick value with highest confidence (1.0 > 0.95 > 0.9)
2. If tied, pick most common value (consensus)
3. List all sources in output
═══════════════════════════════════════════════════════
CRITICAL RULE — DO NOT HALLUCINATE
═══════════════════════════════════════════════════════
- ONLY return attributes that have actual values found in the input data.
- If an attribute has an empty value or was not found in any source, DO NOT include it in the output.
- Do NOT include attributes just because they appear in examples or rules.
- The examples below show HOW to format values WHEN the attribute is present — 
  they are NOT a checklist of attributes you must return.
═══════════════════════════════════════════════════════
═══════════════════════════════════════════════════════
RULES  (read every rule; examples show the exact output required)
═══════════════════════════════════════════════════════
RULE 1 — UNIT ISOLATION  ★ HIGHEST PRIORITY ★
  Move ALL measurement units to the `unit` field.
  `value` must contain ONLY numbers, dimension labels (L/W/H), and connectors (to | x).
  Examples:
    "76 ft."          → value: "76",          unit: "ft"
    "33 m"            → value: "33",          unit: "m"
    "600 V"           → value: "600",         unit: "V"
    "1150 mV"         → value: "1150",        unit: "mV"
    "> 10^6 mOhm"     → value: "> 10^6",      unit: "mOhm"
    "7 mil"           → value: "7",           unit: "mil"
    "14.4 in. lb."    → value: "14.4",        unit: "in-lb"
    "15.4 in-lb"      → value: "15.4",        unit: "in-lb"
    "37 to 55 VDC"    → value: "37 to 55",    unit: "VDC"
    "10V - 12V"       → value: "10 to 12",    unit: "V"
RULE 2 — TEMPERATURE
  Always use "deg C" or "deg F" as the unit. Extract the number only into value.
  Examples:
    "80 Degrees Celsius"   → value: "80",   unit: "deg C"
    "-10 Degrees Celsius"  → value: "-10",  unit: "deg C"
    "-40 to +158 F"        → value: "-40 to 158", unit: "deg F"
    "105 °C"               → value: "105",  unit: "deg C"
RULE 3 — FRACTION → DECIMAL
  Convert all fractions in Length, Width, Height, Size values to decimals.
  Examples:
    "3/4 in."   → value: "0.75",  unit: "in"
    "1-1/2 in." → value: "1.5",   unit: "in"
    "3/8 in."   → value: "0.375", unit: "in"
    "1/2 in."   → value: "0.5",   unit: "in"
RULE 4 — TITLE CASE
  ALL descriptive/categorical text → Title Case. Ignore input casing entirely.
  Examples:
    "BLACK"              → "Black"
    "pvc"                → "PVC"   (abbreviations keep their standard casing)
    "HEAVY DUTY"         → "Heavy Duty"
    "TEMPORARY"          → "Temporary"
    "polyvinyl chloride" → "Polyvinyl Chloride"
RULE 5 — STATUS / BOOLEAN
★ IMPORTANT ★ Only process this rule if a Temporary/Permanent value was 
  actually found in the input data. If no such value exists, skip this attribute entirely.
  For Temporary/Permanent: output ONE status word in Title Case.
  For boolean fields: 1/Y/TRUE → "Yes";  0/N/FALSE → "No".
  Examples:
    "TEMPORARY / PERMANENT"  → "Temporary"
     "TEMPORARY / PERMANENT"  → "PERMANENT"
     "TEMPORARY / PERMANENT"  → "TEMPORARY"
    "PERMANENT"              → "Permanent"
    "Y"                      → "Yes"
    "1"                      → "Yes"
  ★ IMPORTANT ★ For the attribute named "Temporary / Permanent" (or any variant like "Permanent/Temporary"):
    - Output the value as exactly "Temporary" or "Permanent" (Title Case).
    - Map any input variation: "TEMP", "TEMPORARY", "Temporary", "Temporary / Permanent" → "Temporary".
    - Map "PERM", "PERMANENT", "Permanent" → "Permanent".
    - Always include this attribute in the final output if it appears in the input.
RULE 6 — HYPHENS IN DESCRIPTIVE TEXT
  Replace hyphens with spaces in descriptive words (not in technical standards).
  Examples:
    "Double-Sided Tape"  → "Double Sided Tape"
    "Double-Sided"       → "Double Sided"
  Do NOT change: "CSA C22.2", "UL-Listed", "ISO-9001" (technical codes stay).
RULE 7 — MATERIAL MAPPING  (context-aware — ONLY apply if value is a recognized material)
  GUARD: If the value is NOT a known material name or abbreviation (e.g., value is "Tape",
  "Double-Sided Tape", a product name, or anything non-material), set value="" and
  issue_detected=true. Do NOT invent or assume a material.
  ONLY when value IS a recognized material:
  Attribute name is "Material"          → use abbreviation:  "PVC", "Stainless Steel"
  Attribute name is "Backing Material"  → use full name:     "Polyvinyl Chloride"
  Attribute name is "Adhesive Material" → use full name:     "Rubber", "Acrylic"
  Examples:
    Name="Material", Value="pvc"            → "PVC"
    Name="Material", Value="ss"             → "Stainless Steel"
    Name="Material", Value="SS"             → "Stainless Steel"
    Name="Material", Value="Tape"           → value="", issue_detected=true  ← NOT a material
    Name="Material", Value="Double-Sided Tape" → value="", issue_detected=true  ← NOT a material
    Name="Backing Material", Value="pvc"    → "Polyvinyl Chloride"
    Name="Backing Material", Value="PVC"    → "Polyvinyl Chloride"
    Name="Backing Material", Value="Polyvinyl Chloride" → "Polyvinyl Chloride"  (unchanged)
    Name="Adhesive Material", Value="rubber" → "Rubber"
    Name="Adhesive Material", Value="Acrylic" → "Acrylic"
     ★ IMPORTANT ★ For "Adhesive Material", always use the adhesive type (e.g., "Rubber", "Acrylic") – never use the backing material value.
RULE 8 — PLACEHOLDERS → EMPTY
  Convert N/A, None, -, TBD, "null", blank → empty string "".
  Set issue_detected = true for these.
RULE 9 — REDUNDANCY & DEDUPLICATION
  Remove repeated words/phrases inside a single value.
  Strip the attribute name from the value if it appears there.
  Examples:
    "Double-Sided Tape Double Sided Tape" → "Double Sided Tape"
    "Material: Brass"                     → "Brass"
    "Black Black"                         → "Black"
RULE 10 — DIMENSION FORMAT
  Always: "Value L x Value W x Value H", unit in `unit` field.
  Example:
    "4.15 x 2.11 x 1.33 in" → value: "4.15 L x 2.11 W x 1.33 H", unit: "in"
RULE 11 — LIST vs RANGE
  Comma/semicolon separated distinct values → pipe-delimited LIST (never collapse to range).
  Continuous span → use "to".
  Examples:
    "10 Mbps, 100 Mbps, 1000 Mbps" → value: "10 | 100 | 1000", unit: "Mbps"
    "-4 to +140 F"                  → value: "-4 to 140",        unit: "deg F"
RULE 12 — UNIT SYMBOL EXPANSION
  " (inch symbol) → "in"
  mm. → "mm"
  YD / yd → "yd"
  v / V → "V"
  mah → "mAh"
  rpm → "RPM"
  ft. → "ft"
RULE 13 — ROUNDING
  Round decimals to 2 places.  1.998 → 2.00
RULE 13.5 — NORMALIZATION & DEDUPLICATION ★ CRITICAL ★
  Same attribute appearing multiple times with only formatting differences → merge into ONE.
  
  STEP 1: CHARACTER NORMALIZATION (apply first)
    "º" (ordinal U+00BA)     becomes "°" (degree U+00B0)
    smart quotes             becomes straight quotes
    en-dash/em-dash          becomes hyphen
  
  STEP 2: ATTRIBUTE NAME NORMALIZATION
    Word expansion (in attribute names only, not values):
      "Max"  becomes "Maximum"
      "Min"  becomes "Minimum"
      "Temp" becomes "Temperature"
      "Dia"  becomes "Diameter"
      "Qty"  becomes "Quantity"
    
    Degree format standardization (in attribute names):
      "at 90º"   becomes "at 90 Degrees"
      "at 90 Deg" becomes "at 90 Degrees"
      "at 45º"   becomes "at 45 Degrees"
      "at 45 Deg" becomes "at 45 Degrees"
  
  STEP 3: UNIT NORMALIZATION
    deg | degrees | ° becomes deg
    Deg | Degrees (in name) becomes Degrees
    rpm | RPM becomes RPM
    in. | inches becomes in
    lbs | lb | pounds becomes lb
    V | Volts | volts becomes V
  
  STEP 4: MISSING UNIT INFERENCE
    If attribute has NO unit but other instances of same attribute DO have unit,
    infer the most common unit from other instances.
    
    Example:
      No Load RPM: 5500 (no unit)
      No Load RPM: 3650 RPM
      No Load RPM: 3800 (no unit)
    Result: All get unit=RPM
  
  STEP 5: DEDUPLICATION
    After all normalization, if same attribute name + same value, MERGE:
      Keep highest confidence source
      Combine all source URLs
      List all original formats in original_values
  
  Examples:
    Input: Bevel Angle Range with -2 to 47 in deg, degrees, and °
    Output: Single attribute with unit=deg, all sources listed
    
    Input: Max/Maximum Depth at 90 Deg/Degrees/º all with value 3.125 in
    Output: Single Maximum Depth of Cut at 90 Degrees with all sources
RULE 14 — UNIFICATION
  After cleaning, group attributes that represent the same concept
  (e.g., "CCT" and "Color Temperature", "Colour" and "Color").
  Produce ONE output attribute per concept using the most canonical name.
  Pick the value with the highest confidence; list all source URLs and original values.
  ★ IMPORTANT ★ Specific synonym groups:
    - Temperature attributes: treat "Maximum Operating Temperature" and "Operating Temperature - Maximum" as the same.
      If both appear, keep the one that matches the primary attributes list; otherwise keep the one with higher confidence.
    - Material attributes: if both "Material" and "Backing Material" refer to the same material, keep only the one that matches the primary list.
      If neither is in the primary list, keep "Backing Material" (more specific).
═══════════════════════════════════════════════════════
CONCRETE FEW-SHOT EXAMPLES  (exact JSON output required for these inputs)
═══════════════════════════════════════════════════════
Input:  Name="Color",               Value="BLACK"
Output: name="Color", value="Black", unit=null
Input:  Name="Temporary / Permanent", Value="TEMPORARY / PERMANENT"
Output: name="Temporary / Permanent", value="Temporary", unit=null
Input:  Name="Temporary / Permanent", Value="TEMPORARY"
Output: name="Temporary / Permanent", value="Temporary", unit=null
Input:  Name="Backing Material",    Value="pvc"
Output: name="Backing Material", value="Polyvinyl Chloride", unit=null
Input:  Name="Backing Material",    Value="PVC"
Output: name="Backing Material", value="Polyvinyl Chloride", unit=null
Input:  Name="Material",            Value="ss"
Output: name="Material", value="Stainless Steel", unit=null
Input:  Name="Material",            Value="pvc"
Output: name="Material", value="PVC", unit=null
Input:  Name="Width",               Value="3/4 in."
Output: name="Width", value="0.75", unit="in"
Input:  Name="Width",               Value="3/8 in."
Output: name="Width", value="0.375", unit="in"
Input:  Name="Length",              Value="1-1/2 in."
Output: name="Length", value="1.5", unit="in"
Input:  Name="Length",              Value="76 ft."
Output: name="Length", value="76", unit="ft"
Input:  Name="Length",              Value="33 m"
Output: name="Length", value="33", unit="m"
Input:  Name="Thickness",           Value="7 mil"
Output: name="Thickness", value="7", unit="mil"
Input:  Name="Voltage Rating",      Value="600 V"
Output: name="Voltage Rating", value="600", unit="V"
Input:  Name="Dielectric Strength", Value="1150 mV"
Output: name="Dielectric Strength", value="1150", unit="mV"
Input:  Name="Insulation Resistance", Value="> 10^6 mOhm"
Output: name="Insulation Resistance", value="> 10^6", unit="mOhm"
Input:  Name="Operating Temperature - Maximum", Value="80 Degrees Celsius"
Output: name="Operating Temperature - Maximum", value="80", unit="deg C"
Input:  Name="Operating Temperature - Minimum", Value="-10 Degrees Celsius"
Output: name="Operating Temperature - Minimum", value="-10", unit="deg C"
Input:  Name="Tensile Strength",    Value="14.4 in. lb."
Output: name="Tensile Strength", value="14.4", unit="in-lb"
Input:  Name="Tensile Strength",    Value="15.4 in-lb"
Output: name="Tensile Strength", value="15.4", unit="in-lb"
Input:  Name="Product Type",        Value="Double-Sided Tape"
Output: name="Product Type", value="Double Sided Tape", unit=null
Input:  Name="Product Type",        Value="DOUBLE SIDED TAPE"
Output: name="Product Type", value="Double Sided Tape", unit=null
Input:  Name="RoHS Compliant",      Value="YES"
Output: name="RoHS Compliant", value="Yes", unit=null
Input:  Name="RoHS Compliant",      Value="No"
Output: name="RoHS Compliant", value="No", unit=null
Input:  Name="Size",                Value="LxDia: 36 x 3/8 in."
Output: name="Size", value="36 L x 0.375 W", unit="in"
Input:  Name="Special Rating",      Value="UL;CSA C22.2"
Output: name="Special Rating", value="UL; CSA C22.2", unit=null
═══════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════
Return a JSON object:
{{
  "attributes": [
    {{
      "name": "Canonical Attribute Name",
      "value": "cleaned value (NO unit string here if unit field is populated)",
      "unit": "unit string or null",
      "confidence": 0.95,
      "sources": ["https://source1.com"],
      "original_values": ["original raw value"]
    }}
  ],
  "summary": "Brief explanation of major changes and grouping decisions."
}}
CRITICAL FINAL CHECKS before returning:
  ✓ Does any value still contain its unit string?  If yes → fix it.
  ✓ Are fractions still present in Length/Width/Size?  If yes → convert to decimal.
  ✓ Is any descriptive text still ALL CAPS or all lowercase?  If yes → Title Case it.
  ✓ Does "Backing Material" or "Adhesive Material" still say "PVC" or "ss"?  If yes → expand to full name.
  ✓ Does "Material" say "Polyvinyl Chloride"?  If yes → shorten to "PVC".
  ✓ Is "Temporary / Permanent" outputting two words?  If yes → pick one.
  ✓ Are hyphens still in descriptive words like "Double-Sided"?  If yes → replace with space.
  ✓ Does any Material/Backing Material/Adhesive Material have a non-material value (product name, tape type, etc.)?  If yes → set value="" and issue_detected=true.
    ✓ Is "Temporary / Permanent" correctly title-cased and present? If missing, ensure it's included (set value="" if not found).
  ✓ Does "Adhesive Material" say "PVC"? If yes → it is wrong; set to empty or correct value if possible (look for a source that says "Rubber").
  ✓ Have all primary attributes been considered? If a primary attribute is missing because no matching specification was found, still include it with an empty value and note in original_values that it was not found.
{excel_section}
{validation_section}
═══════════════════════════════════════════════════════
INPUT ATTRIBUTES
═══════════════════════════════════════════════════════
{attributes_text}
"""
    return prompt
