import re
from typing import Dict, List, Optional
from sqlalchemy import func
import time
from app.core.config import settings
from app.llm import call_llm_with_schema
from typing import Dict, Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import or_, select
from app.aggregation.prompts.enrichment_prompts import build_enrichment_prompt
from app.aggregation.services.pdf_service import PDFExtractionService
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
from app.models.pdf_validation import PdfValidation
from app.models.product_attribute_link import ProductAttributeValueLinkModel
from app.models.brand import Brand
from app.models.attribute import Attribute, AttributeValue, CategoryAttribute
from app.aggregation.services.extraction_service import (
    ExtractionService, HtmlExtractor, PdfExtractor, PlaywrightExtractor)
from app.models.product import Product
from app.models.project import Project
import asyncio
from uuid import UUID as _UUID
from app.services.crawl4ai_service import render_with_crawl4ai
from app.utils.attribute_filters import is_distributor_metadata
from firecrawl import Firecrawl
from app.aggregation.services.image_service import extract_best_image, extract_best_image_fallback
import logging
from app.rules.rule_engine import RuleEngine
from app.services.canonical_alias_service import enqueue_category_alias_job
from app.services.category_canonical_resolver import load_category_canonical_winners
from app.services.product_discovery_service import ProductDiscoveryService
from app.utils.image_validator import extract_mozu_preload_images, extract_universal_product_images, is_manufacturer_domain, validate_image_url
from app.utils.normalization_helper import _standardize_uom_in_attrs, normalize_concatenated_uom
from app.utils.pdf_utils import _build_pdf_prompt, is_crossref_pdf, is_parts_list_pdf
from app.utils.remapping import cluster_attributes_by_meaning
download_service = HttpDownloadService(timeout=20)

logger = logging.getLogger("aggregate_product")


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
    if not prompt_text:
        return []
    url_pattern = r'https?://[^\s\)\"]+'
    return re.findall(url_pattern, prompt_text)


async def discover_manufacturer_urls(
    brand: str,
    taxonomy: str,
    mpn: str,
    llm_provider: str = 'openai',
    title: Optional[str] = None,
) -> List[str]:
    import httpx
    from urllib.parse import urlparse
    logger.info(f"Discovering manufacturer URLs for {brand} ({taxonomy})")
    KNOWN_MANUFACTURERS = {
        "milton": [
            "https://miltonindustries.com/products/pistol-grease-gun-high-pressure-high-volume",
            "https://miltonindustries.com"
        ],
        "graco": ["https://graco.com"],
        "milwaukee": ["https://milwaukeetool.com"],
        "craftsman": ["https://craftsman.com"],
    }
    taxonomy_parts = taxonomy.split(" > ")
    main_category = taxonomy_parts[-1] if taxonomy_parts else taxonomy
    is_mpn_valid = mpn and str(mpn).strip().lower() != 'none'
    if is_mpn_valid:
        search_queries = [
            f"{brand} {main_category} official website",
            f"{brand} {mpn} manufacturer",
        ]
    else:
        title_snippet = " ".join(title.split()[:3]) if title else ""
        search_queries = [
            f"{brand} {title_snippet} official website",
            f"{brand} {main_category} official site",
            f"{brand} {title_snippet} manufacturer",
        ]
    brand_lower = brand.lower()
    if brand_lower in KNOWN_MANUFACTURERS:
        urls = KNOWN_MANUFACTURERS[brand_lower]
        logger.info(f"Using known manufacturer URLs: {urls}")
        return urls
    try:
        from app.aggregation.services.download_service import HttpDownloadService
        # download_service = HttpDownloadService(timeout=20)
        manufacturer_urls = []
        for query in search_queries:
            try:
                logger.info(f"Searching: {query}")
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.get(
                        f"{settings.SEARXNG_URL}/search",
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
                    logger.info(f"No results for: {query}")
                    continue
                logger.info(f"Found {len(results)} results")
                for result in results:
                    url = result.get("url", "")
                    title = result.get("title", "").lower()
                    if not url:
                        continue
                    blocked_keywords = [
                        '/search', '/results', '/compare', '/category',
                        'amazon', 'ebay', 'walmart', 'target', 'alibaba',
                        'aliexpress', 'pinterest', 'youtube', 'linkedin',
                        'twitter', 'facebook', 'instagram', 'reddit',
                        '.gov', '.edu', 'wikipedia', '/blog'
                    ]
                    if any(keyword in url.lower() for keyword in blocked_keywords):
                        continue
                    domain = urlparse(url).netloc.lower()
                    if brand_lower not in domain:
                        continue
                    manufacturer_urls.append(url)
                    logger.info(f"✓ Found candidate: {url}")
                if manufacturer_urls:
                    break
            except Exception as e:
                logger.info(f"Query failed: {e}")
                continue
        if not manufacturer_urls:
            logger.warning(f"No manufacturer URLs found for {brand}")
            return []
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
                logger.info(f"Verification failed: {e}")
                continue
        final_urls = []
        for url in product_urls:
            if is_mpn_valid and mpn.lower() in url.lower():
                final_urls.append(url)
            elif not is_mpn_valid:
                final_urls.append(url)
            logger.info(
                f"Found product URL for {mpn if is_mpn_valid else 'title'}: {url}")
        if not final_urls:
            final_urls.extend(product_urls)
        if not final_urls:
            final_urls.extend(verified_urls)
        if not final_urls:
            final_urls.extend(homepage_urls)
        if not final_urls and (product_urls or verified_urls):
            logger.info(
                f"Using LLM to rank {len(product_urls + verified_urls)} URLs")
            identifier = mpn if is_mpn_valid else title
            ranking_prompt = f"""
            Identify the official manufacturer product page URL for {brand} {identifier}.
            Product category: {main_category}
            Product Identifier: {identifier}
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
    is_mpn_valid = mpn and str(mpn).strip().lower() != 'none'
    if is_mpn_valid:
        search_queries = [f"{brand} {mpn}", mpn, f"{brand} {title.split()[0]}"]
    else:
        search_queries = [
            f"{brand} {title}",
            title
        ]
    import httpx
    from urllib.parse import urlparse, urljoin
    import re
    try:
        domain = urlparse(domain_url).netloc.replace('www.', '')
        if is_mpn_valid:
            search_queries = [f"{brand} {mpn}",
                              mpn, f"{brand} {title.split()[0]}"]
        else:
            search_queries = [f"{brand} {title}", title]
        if taxonomy:
            taxonomy_parts = taxonomy.split(" > ")
            main_category = taxonomy_parts[-1] if taxonomy_parts else taxonomy
            mpn_suffix = f" {mpn}" if is_mpn_valid else ""
            search_queries.insert(0, f"{brand} {main_category}{mpn_suffix}")
            search_queries.insert(
                1, f"{main_category} {mpn if is_mpn_valid else ''}".strip())
            search_queries.insert(2, f"{brand} {main_category}")
        logger.info(
            f"Searching {domain} for identifier: {mpn if is_mpn_valid else title}")
        from app.aggregation.services.download_service import HttpDownloadService
        # download_service = HttpDownloadService(timeout=20)
        for query in search_queries:
            try:
                logger.info(f"Query: {query}")
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.get(
                        f"{settings.SEARXNG_URL}/search",
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
                    logger.info(f"No results for: {query}")
                    continue
                logger.info(f"Found {len(results)} general results")
                product_urls = []
                for result in results:
                    url = result.get("url", "")
                    result_title = result.get("title", "").lower()
                    if domain not in url:
                        continue
                    blocked_paths = ['/search', '/results', '/browse', '/category',
                                     '/categories', '/brand', '/brands', '/blog',
                                     '/news', '/about', '/register', '/login',
                                     '/cart', '/checkout', '/compare']
                    if any(blocked in url.lower() for blocked in blocked_paths):
                        continue
                    url_lower = url.lower()
                    has_brand = brand.lower() in url_lower
                    has_title = title.lower() in url_lower or any(
                        word in url_lower for word in title.lower().split() if len(word) > 3
                    )
                    has_mpn = mpn.lower() in url_lower
                    if not (has_brand or has_title or has_mpn):
                        continue
                    product_urls.append(url)
                if not product_urls:
                    logger.info(f"No product URLs for query: {query}")
                    continue
                logger.info(
                    f"Found {len(product_urls)} candidate product URLs")
                priority_urls = []
                homepage_urls = []
                category_urls = []
                for url in product_urls:
                    url_lower = url.lower()
                    parsed = urlparse(url)
                    if '/product/' in url_lower or '/products/' in url_lower:
                        priority_urls.append(url)
                    elif parsed.path in ['', '/']:
                        homepage_urls.append(url)
                    elif '/category/' in url_lower or '/collection/' in url_lower:
                        category_urls.append(url)
                    else:
                        priority_urls.append(url)
                sorted_urls = priority_urls + category_urls + homepage_urls
                logger.info(
                    f"Sorted URLs: {len(priority_urls)} priority, {len(category_urls)} category, {len(homepage_urls)} homepage")
                for url in sorted_urls:
                    try:
                        logger.info(f"Verifying: {url}")
                        content = await download_service.download(url)
                        if content and content['type'] == 'html':
                            html = content['raw_bytes'].decode(
                                'utf-8', errors='ignore').lower()
                            has_mpn_in_html = (
                                mpn.lower() in html) if is_mpn_valid else False
                            has_brand_in_html = brand.lower() in html
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
                            title_keywords = [
                                w for w in title.lower().split() if len(w) > 3]
                            title_hits = sum(
                                1 for kw in title_keywords if kw in html)
                            has_title_density = (
                                title_hits / len(title_keywords) >= 0.5) if title_keywords else False
                            parsed_url = urlparse(url)
                            is_homepage = parsed_url.path in ['', '/']
                            is_product_url = '/product/' in url.lower() or '/products/' in url.lower()
                            url_path = urlparse(url).path.strip('/')
                            last_segment = url_path.split('/')[-1]
                            hyphen_count = last_segment.count('-')
                            if hyphen_count <= 3 and not is_product_url:
                                category_indicators = [
                                    'sort by', 'filter by', 'items per page', 'showing 1-',
                                    'compare products', 'grid view', 'list view'
                                ]
                                category_hits = sum(
                                    1 for ind in category_indicators if ind in html)
                                if category_hits >= 1:
                                    logger.info(
                                        f"Rejecting category/listing page (Generic URL + List indicators found): {url}")
                                    continue
                            if is_mpn_valid:
                                if is_product_url and (has_mpn_in_html or indicator_count >= 1):
                                    logger.info(
                                        f"✓ VERIFIED product page (MPN/Heuristic): {url}")
                                    return url
                                if has_mpn_in_html and not is_homepage:
                                    logger.info(
                                        f"✓ VERIFIED page (MPN found): {url}")
                                    return url
                            else:
                                if is_product_url and has_brand_in_html and has_title_density:
                                    logger.info(
                                        f"✓ VERIFIED via title density: {url}")
                                    return url
                                if not is_homepage and has_brand_in_html and indicator_count >= 2:
                                    logger.info(
                                        f"✓ VERIFIED via brand + indicators: {url}")
                                    return url
                    except Exception as e:
                        logger.info(f"Verification failed: {e}")
                        continue
                if priority_urls:
                    logger.info(
                        f"✓ Using first priority URL (unverified): {priority_urls[0]}")
                    return priority_urls[0]
                elif product_urls:
                    logger.info(
                        f"✓ Using first matching URL (unverified): {product_urls[0]}")
                    return product_urls[0]
            except Exception as e:
                logger.info(f"Query failed: {e}")
                continue
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
                    all_links = re.findall(r'href="([^"]*)"', html)
                    product_candidates = []
                    for link in all_links:
                        link_lower = link.lower()
                        if any(x in link_lower for x in ['/search', '/category', '/brand', '/compare']):
                            continue
                        if mpn.lower() in link_lower or brand.lower() in link_lower:
                            product_candidates.append(link)
                    logger.info(
                        f"Found {len(product_candidates)} product candidates")
                    product_candidates.sort(
                        key=lambda x: 0 if '/product/' in x.lower() else 1)
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
                                if is_product_url and (has_mpn or has_brand):
                                    logger.info(
                                        f"✓ VERIFIED fallback product page: {candidate_url}")
                                    return candidate_url
                                if (has_mpn or has_brand) and indicator_count >= 1:
                                    logger.info(
                                        f"✓ VERIFIED fallback product page: {candidate_url}")
                                    return candidate_url
                        except Exception as e:
                            logger.info(f"Fallback verification failed: {e}")
                            continue
                    if product_candidates:
                        final_url = product_candidates[0]
                        if not final_url.startswith('http'):
                            final_url = urljoin(domain_url, final_url)
                        logger.info(
                            f"✓ Using fallback URL (unverified): {final_url}")
                        return final_url
            except Exception as e:
                logger.info(f"Domain fallback search failed: {e}")
        logger.warning(f"Could not find product page on {domain} for {mpn}")
        return None
    except Exception as e:
        logger.warning(f"Product discovery failed: {e}")
        return None


def _url_contains_product_identifier(url: str, mpn: str, title: str) -> bool:
    if not url:
        return False
    path = urlparse(url).path.lower()
    if mpn and str(mpn).strip().lower() not in ['none', '', 'unk']:
        if str(mpn).lower() in path:
            return True
    if title:
        words = [w.lower() for w in title.split() if len(w) > 3]
        if not words:
            return False
        hits = sum(1 for w in words if w in path)
        if len(words) == 1 and hits >= 1:
            return True
        if len(words) >= 2 and hits >= 2:
            return True
    return False


def is_result_actually_product(result: dict, brand: str, title: str, mpn: str = None) -> bool:
    url = result.get("url", "").lower()
    if mpn and mpn.lower() in url:
        return True
    content_to_check = (result.get('title', '')+" " +
                        result.get('snippet', "")).lower()
    brand_lower = brand.lower()

    if brand_lower not in content_to_check and brand_lower not in url:
        return False
    snippet = (result.get("title", "") + " " +
               result.get("snippet", "")).lower()
    if brand.lower() not in snippet and brand.lower() not in url.lower():
        return False
    stop_words = {'with', 'high', 'sides', 'tray',
                  'for', 'the', 'this', 'and', 'from', 'each'}
    name_tokens = [
        w.lower() for w in title.split()
        if len(w) > 2 and w.lower() not in stop_words
    ]
    if not name_tokens:
        return brand_lower in content_to_check
    matches = sum(
        1 for token in name_tokens if token in content_to_check or token in url)
    match_ratio = matches / len(name_tokens)
    if match_ratio >= 0.5:
        return True
    return False


def _base_model_tokens_match(mpn: str, url: str) -> bool:
    if not mpn or not url:
        return False
    mpn_upper = re.sub(r'[^A-Z0-9]', '', mpn.upper())
    url_slug = re.sub(r'[^A-Z0-9]', '', urlparse(url).path.upper())
    m = re.match(r'^([A-Z]+\d+)([A-Z]?)([A-Z]+)(\d{3})([A-Z]*)$', mpn_upper)
    if not m:
        return mpn_upper in url_slug or url_slug in mpn_upper
    base, variant_letter, style, finish, suffix = m.groups()
    base_style = base + style
    prefix_ok = base_style in url_slug
    suffix_ok = (not suffix) or (suffix in url_slug)
    return prefix_ok and suffix_ok


async def verify_page_identity_with_llm(url: str, html: str, brand: str, title: str, mpn: str, llm_provider: str, manufacturer_domain: str) -> bool:
    from bs4 import BeautifulSoup
    from app.core.config import settings
    import logging
    logger = logging.getLogger("aggregate_product")
    is_mfr_url = (
        manufacturer_domain and
        manufacturer_domain.replace("https://", "").replace("www.", "") in url
    )
    if is_mfr_url and _base_model_tokens_match(mpn, url):
        logger.info(f"✓ Base token match on mfr domain, skip LLM: {url}")
        return True
    soup = BeautifulSoup(html, 'html.parser')
    for junk in soup(["script", "style", "nav", "footer", "header", "aside"]):
        junk.decompose()
    page_text = soup.get_text(separator=' ', strip=True)
    # if len(page_text) < 500:
    #     logger.info(
    #         f"Verification text for {url} is too thin. Attempting Firecrawl rendering...")
    #     try:
    #         fc_client = Firecrawl(api_key=settings.FIRECRAWL_API_KEY)
    #         fc_result = fc_client.scrape_url(url)
    #         rendered_text = ""
    #         if isinstance(fc_result, dict):
    #             rendered_text = fc_result.get(
    #                 'markdown') or fc_result.get('content') or ""
    #         else:
    #             rendered_text = getattr(fc_result, 'markdown', '') or getattr(
    #                 fc_result, 'content', '') or ""
    #         if rendered_text:
    #             page_text = rendered_text
    #     except Exception as e:
    #         logger.warning(f"Firecrawl fallback failed: {e}")
    if len(page_text) < 500:
        logger.info(
            f"Verification text for {url} is too thin ({len(page_text)} chars). "
            f"Attempting Crawl4AI rendering..."
        )
        try:
            if getattr(settings, "USE_CRAWL4AI", True):
                ca_html = await render_with_crawl4ai(url)
                if ca_html and len(ca_html) > 300:
                    logger.info(
                        f"[Crawl4AI] Rendered content for verification - size: {len(ca_html)} bytes"
                    )
                    ca_soup = BeautifulSoup(ca_html, 'html.parser')
                    for junk in ca_soup(["script", "style", "nav", "footer", "header", "aside"]):
                        junk.decompose()
                    rendered_text = ca_soup.get_text(separator=' ', strip=True)
                    if rendered_text and len(rendered_text) > len(page_text):
                        page_text = rendered_text
                else:
                    logger.warning(
                        "[Crawl4AI] Returned empty/small result for verification. "
                        "Proceeding with original HTML text."
                    )
            else:
                logger.info("[Crawl4AI] Disabled by config; skipping.")
        except Exception as e:
            logger.warning(f"Crawl4AI fallback in verification failed: {e!r}")
    snippet = page_text[:6000]
    prompt = f"""
    You are a Senior Product Data Matcher. Verify if the following web page is the official Product Detail Page (PDP) for the requested item.
    TARGET PRODUCT:
    - Brand: {brand}
    - Title: {title}
    - Target MPN/ID: {mpn}
    CANDIDATE PAGE TO EVALUATE:
    - URL: {url}
    - Page Content Snippet: {snippet}
    VERIFICATION RULES:
    1. MATCH if the Brand '{brand}' is the primary subject of the page.
    2. MATCH if the base model string appears in the URL, even if finish/variant suffix differs.
       HOW TO CHECK:
       - Extract the alphabetic+numeric base from the MPN (ignore single letters, ignore 3-digit finish codes like 716, 619, 619ACC)
       - Check if that base appears anywhere in the candidate URL
       EXAMPLES (all should be MATCH):
       - MPN: BE365VCAM716     → base: BE365CAM  → URL has BE365CAMFFF     ✓ MATCH
       - MPN: FE595VCAM716ACC  → base: FE595CAM  → URL has FE595CAMFFFACC  ✓ MATCH  
       - MPN: FE595VCAM619ACC  → base: FE595CAM  → URL has FE595CAMFFFACC  ✓ MATCH
       - MPN: ND80PD-RH-619    → base: ND80PD    → URL has ND80PD-RH       ✓ MATCH
       RULE: If ≥60% of the MPN base characters appear in sequence in the URL slug → MATCH.
       Do NOT reject just because the finish code (3 digits) or variant prefix (single letter V/B/F) is missing.
    3. MATCH if the product name describes the exact same physical item (Example: 'GTPOBUS1225' matches 'Gozney Tread Oven').
    4. REJECT (False) if it is a Category or Search Results page showing multiple different products.
    5. REJECT (False) if it is an Installation Manual, Support PDF, News Article, or Store Locator.
    6. REJECT (False) if the page says "Product Not Found" or is a 404 error.
    Return your decision in strict JSON format.
    """
    try:
        from app.llm import call_llm_with_schema
        res = await call_llm_with_schema(prompt, "IdentityVerificationResponse", llm_provider)
        if res:
            logger.info(
                f"AI Verification Result for {url}: {res.is_match} (Confidence: {res.confidence}) - Reasoning: {res.reasoning}")
            return res.is_match if res.confidence > 0.7 else False
        return False
    except Exception as e:
        logger.error(f"LLM Identity Verification failed for {url}: {e}")
        return False


async def _enqueue_alias_job_isolated(category_id):
    from app.core.database import async_session_factory

    try:
        async with async_session_factory() as new_db:
            await enqueue_category_alias_job(category_id, new_db)
    except Exception as e:
        logger.warning(f"[Stage3Canonicals] isolated alias job failed: {e}")


async def extract_approved_pdf(
    validation: "PdfValidation",
    db: AsyncSession,
    llm_provider: str = "openai",
) -> Dict:
    try:
        from app.api.v1.endpoints.aggregation import get_product_attributes_for_aggregation
        pdf_content = await download_service.download(validation.pdf_url)
        if not pdf_content or pdf_content.get("type") != "pdf":
            return {"status": "failed", "reason": "PDF download failed"}

        pdf_service = PDFExtractionService(max_pages=10)
        pdf_text = await pdf_service.extract_text(pdf_content["raw_bytes"])
        if not pdf_text or len(pdf_text.strip()) < 100:
            return {"status": "failed", "reason": "PDF text extraction failed"}

        prod_stmt = select(Product).where(
            Product.product_code == validation.product_code)
        prod_res = await db.execute(prod_stmt)
        product = prod_res.scalars().first()
        if not product:
            return {"status": "failed", "reason": "Product not found"}

        primary_attrs, _ = await get_product_attributes_for_aggregation(db, product)

        prompt_config = _build_pdf_prompt(
            pdf_text=pdf_text,
            title=product.product_name,
            mpn=product.product_code,
            brand=product.brand_name,
            taxonomy=product.taxonomy,
            primary_attributes=primary_attrs,
            attribute_chunk=None,
        )

        extraction_result = await call_llm_with_schema(
            prompt=prompt_config["prompt"],
            response_model="ExtractionResponse",
            llm_provider=llm_provider,
            estimated_tokens=4000,
        )

        if not extraction_result or not extraction_result.product_detected:
            return {"status": "failed", "reason": "Product not detected in PDF"}

        attr_dicts = []
        for attr in extraction_result.attributes:
            if is_distributor_metadata(attr.name):
                continue
            attr_dicts.append({
                "name": attr.name,
                "value": attr.value,
                "unit": getattr(attr, "unit", None),
                "confidence": getattr(attr, "confidence", 0.95),
            })

        return {
            "status": "success",
            "attributes": attr_dicts,
            "image_urls": list(getattr(extraction_result, "image_urls", None) or []),
            "product": product,
        }
    except Exception as e:
        logger.error(f"extract_approved_pdf failed: {e}", exc_info=True)
        return {"status": "failed", "reason": str(e)}


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
    missing_llm_provider: str = None,
    is_algo2_run: bool = False,
    cached_html: Optional[Dict[str, str]] = None,
    cached_urls: Optional[List[str]] = None,
) -> Dict:
    try:
        primary_attributes = [
            name
            for name in (primary_attributes or [])
            if not is_distributor_metadata(name)
        ]

        if attribute_chunk:
            attribute_chunk = [
                name
                for name in attribute_chunk
                if not is_distributor_metadata(name)
            ]

        if existing_excel_attrs:
            existing_excel_attrs = {
                name: value
                for name, value in existing_excel_attrs.items()
                if not is_distributor_metadata(name)
            }
        pipeline_start = time.perf_counter()
        if missing_llm_provider is None:
            missing_llm_provider = llm_provider
        candidate_images = []
        # found_image_assets_global = []
        found_image_assets_global: list[dict] = []

        def _merge_images(image_urls: Optional[List[str]], source_page_url: str, source_type: str, is_primary: bool = False):
            if not image_urls:
                return
            seen_urls = {a['image_url']
                         for a in found_image_assets_global if a.get('image_url')}
            for idx, u in enumerate(image_urls):
                if not u or u in seen_urls:
                    continue

                found_image_assets_global.append({
                    "image_url": u,
                    "source_page_url": source_page_url,
                    "source_type": source_type,
                    "is_primary": is_primary and idx == 0,
                })
                seen_urls.add(u)
            del found_image_assets_global[8:]
        all_extractions = []
        if is_algo2_run and cached_urls and cached_html:
            logger.info(
                f"Algo 2: Skipping Stage 1-2, reusing {len(cached_urls)} cached URLs"
            )
            urls = cached_urls
            all_extractions = []
            for url in urls:
                if url not in cached_html:
                    logger.warning(
                        f"Algo 2: Cached HTML missing for {url}, skipping")
                    continue
                html_text = cached_html[url]
                logger.info(
                    f"Algo 2: Using cached HTML for {url} - size: {len(html_text)} bytes"
                )
                attrs_to_use = primary_attributes or []
                prompt_config = build_extraction_prompt(
                    product_name=title,
                    mpn=mpn,
                    brand=brand or "",
                    taxonomy=taxonomy or "",
                    primary_attributes=attrs_to_use,
                    html_content=html_text,
                    candidate_images=[],
                    source_url=url
                )
                llm_start = time.perf_counter()

                extraction_result = await call_llm_with_schema(
                    prompt=prompt_config['prompt'],
                    response_model="ExtractionResponse",
                    llm_provider=llm_provider,
                    estimated_tokens=3000
                )
                logger.info(
                    "HTML extraction LLM for %s took %.2fs",
                    url,
                    time.perf_counter() - llm_start,
                )
                attr_dicts = []
                extracted_upc = None
                extracted_ean = None
                extracted_gtin = None

                if extraction_result and extraction_result.product_detected:
                    _merge_images(getattr(extraction_result,
                                  "image_urls", None) or [], url, 'html')
                    extracted_upc = getattr(extraction_result, "upc", None)
                    extracted_ean = getattr(extraction_result, "ean", None)
                    extracted_gtin = getattr(extraction_result, "gtin", None)
                    for attr in extraction_result.attributes:
                        if is_distributor_metadata(attr.name):
                            logger.info(
                                "Skipped distributor metadata during cached extraction: %s=%s",
                                attr.name,
                                attr.value,
                            )
                            continue
                        attr_dicts.append({
                            'name': attr.name,
                            'value': attr.value,
                            'unit': attr.unit if hasattr(attr, 'unit') else None,
                            'confidence': attr.confidence if hasattr(attr, 'confidence') else 0.9
                        })
                all_extractions.append({
                    'url': url,
                    'domain': urlparse(url).netloc,
                    'attributes': attr_dicts,
                    'source_type': 'html',
                    'upc': extracted_upc,
                    'ean': extracted_ean,
                    'gtin': extracted_gtin,
                })
            logger.info(
                f"Algo 2: Extracted {len(all_extractions)} sources from cached HTML"
            )
            if not all_extractions:
                return {
                    'status': 'failed',
                    'reason': 'No valid cached extractions',
                    'golden_record': {'attributes': {}}
                }
        else:
            stage1_start = time.perf_counter()

            logger.info("Stage 1: URL Discovery")

            logger.info(f"Starting aggregation for {mpn}")
            # download_service = HttpDownloadService(timeout=30)
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
                    for cat in categories:
                        prompt_stmt = select(CategoryPrompt).where(
                            CategoryPrompt.category_id == cat.id,
                            CategoryPrompt.status == RuleStatus.ACTIVE,
                            CategoryPrompt.selected_taxonomy.is_(None)
                        ).limit(1)
                        prompt_result = await db.execute(prompt_stmt)
                        cat_prompt = prompt_result.scalars().first()
                        if cat_prompt:
                            category_prompt_text = cat_prompt.prompt_text
                            matched_category_id = cat.id
                            logger.info(
                                f"✓ Category prompt matched: '{cat.name}'")
                            break
                if not category_prompt_text:
                    logger.info(
                        f"✗ No prompt found for taxonomy: '{clean_taxonomy}'")
            search_service = SmartSearchService(
                llm_provider, db=db, searxng_url=settings.SEARXNG_URL, max_results=5)
            query = title if (
                mpn in title and brand in title) else f"{brand} {mpn} {title}"
            query = query.strip()
            direct_domains = set()
            direct_urls = []
            discovery_service = ProductDiscoveryService(max_results=5)
            if brand:
                manufacturer_start = time.perf_counter()

                manufacturer_domain = await discovery_service.discover_manufacturer_domain(
                    brand=brand,
                    category=taxonomy,
                    db=db,
                    title=title
                )
                logger.info(
                    "[TIMING] Manufacturer discovery: %.2fs",
                    time.perf_counter() - manufacturer_start,
                )
                if manufacturer_domain:
                    logger.info(
                        f"✓ Manufacturer domain found: {manufacturer_domain}")
                    direct_domains.add(urlparse(manufacturer_domain).netloc)
            if brand_prompt_text:
                brand_urls = extract_urls_from_prompt(brand_prompt_text)
                if not brand_urls:
                    brand_urls = extract_domains_and_generate_urls(
                        brand_prompt_text)
                for url in brand_urls:
                    direct_domains.add(urlparse(url).netloc)
            elif category_prompt_text:
                category_urls = extract_urls_from_prompt(category_prompt_text)
                if not category_urls:
                    category_urls = extract_domains_and_generate_urls(
                        category_prompt_text)
                for url in category_urls:
                    direct_domains.add(urlparse(url).netloc)
            for domain in direct_domains:
                logger.info(f"Searching product page on domain: {domain}")
                find_page_start = time.perf_counter()

                product_url = await discovery_service.find_product_page(
                    domain=f"https://{domain}",
                    brand=brand,
                    mpn=mpn,
                    title=title,
                    taxonomy=taxonomy
                )
                logger.info(
                    "[TIMING] find_product_page (%s): %.2fs",
                    domain,
                    time.perf_counter() - find_page_start,
                )
                if product_url:
                    if _url_contains_product_identifier(product_url, mpn, title):
                        logger.info(
                            f"✓ Found product page via strict match: {product_url}")
                        direct_urls.append(product_url)
                    else:
                        logger.info(
                            f"Strict match failed for {product_url}. Verifying semantically...")
                        content = await download_service.download(product_url)
                        if content and content.get("type") == "html":
                            html_text = content["raw_bytes"].decode(
                                "utf-8", errors="ignore")
                            verify_start = time.perf_counter()

                            is_match = await verify_page_identity_with_llm(
                                url=product_url,
                                html=html_text,
                                brand=brand,
                                title=title,
                                mpn=mpn,
                                llm_provider=llm_provider,
                                manufacturer_domain=manufacturer_domain
                            )
                            logger.info(
                                "[TIMING] LLM page verification: %.2fs",
                                time.perf_counter() - verify_start,
                            )
                            if is_match:
                                logger.info(
                                    f"✓ AI confirmed this is the correct product line: {product_url}")
                                direct_urls.append(product_url)
                            else:
                                logger.warning(
                                    f"⛔ AI rejected this page: {product_url}")
            seen = set()
            direct_urls = [u for u in direct_urls if not (
                u in seen or seen.add(u))]
            if direct_urls:
                logger.info(
                    f"Final direct product URLs for {mpn}: {direct_urls}")
            is_mpn_valid = mpn and str(mpn).strip().lower() != 'none'
            clean_mpn = mpn if is_mpn_valid else ""
            if is_mpn_valid:
                query = title if (
                    clean_mpn in title and brand in title) else f"{brand} {clean_mpn} {title}"
            else:
                query = f"{brand} {title}" if brand and brand.lower(
                ) not in title.lower() else title
            query = query.strip()
            logger.info(
                f"Generated search query: {query} (MPN Valid: {is_mpn_valid})")
            if cached_urls and len(cached_urls) > 0:
                logger.info("Multi-pass: Reusing previously discovered URLs")
                urls = list(cached_urls)
                candidate_images = []
            else:
                smart_search_start = time.perf_counter()

                urls, candidate_images = await search_service.get_urls(
                    query, mpn=mpn if is_mpn_valid else "", brand=brand, sku=sku,
                    brand_prompt_text=brand_prompt_text,
                    category_prompt_text=category_prompt_text,
                    taxonomy=taxonomy, direct_urls=direct_urls, selected_taxonomy=selected_taxonomy, title=title
                )
                logger.info(
                    "[TIMING] Smart search: %.2fs",
                    time.perf_counter() - smart_search_start,
                )
            manufacturer_has_product = len(direct_urls) > 0
            if not urls:
                logger.info(
                    f"Smart search found no URLs for {mpn}. Trying fallback searches...")
                import httpx as _httpx
                BLOCKED_FALLBACK = [
                    "youtube.com", "facebook.com", "twitter.com", "reddit.com",
                    "wikipedia.org", "pinterest.com", "instagram.com", "linkedin.com",
                    "zhihu.com", "baidu.com", "weibo.com", "quora.com",
                    "amazon.com", "ebay.com", "walmart.com",
                    "play.google.com", "apps.apple.com",
                    "loving-newyork.com", "visitlondon.com", "nascom.nasa.gov",
                    "sohostudiocorp.com", "nyctourism.com", "usatoday.com",
                    "msn.com", "tripadvisor.com", "timeout.com",
                    "officelabo.net", "extendoffice.com", "prau-pc.jp",
                    "excel-", "tutorial", "qa108", "outline-delete"
                ]
                fallback_queries = []
                is_retailer_sku = is_mpn_valid and str(
                    mpn).isdigit() and len(str(mpn)) == 10
                if manufacturer_has_product and direct_domains:
                    if is_mpn_valid:
                        fallback_queries.append(
                            f'"{mpn}" site:{list(direct_domains)[0]}')
                    if title:
                        fallback_queries.append(
                            f"{title} site:{list(direct_domains)[0]}")
                else:
                    if is_retailer_sku:
                        fallback_queries.append(f"{brand} {title}")
                        fallback_queries.append(f"{brand} {title} buy")
                        fallback_queries.append(f"{title} {brand} shop")
                    else:
                        logger.info(
                            "Manufacturer domain has no product page. Searching broader web...")
                        if is_mpn_valid and title:
                            fallback_queries.append(f'{mpn} {title}')
                        if is_mpn_valid:
                            fallback_queries.append(f'{mpn} {brand} buy')
                            fallback_queries.append(f'{mpn}')
                        if title:
                            fallback_queries.append(f"{brand} {title}")
                            fallback_queries.append(f"{title} {brand} shop")
                for fb_query in fallback_queries:
                    if urls:
                        break
                    try:
                        logger.info(f"Fallback query: '{fb_query}'")
                        async with _httpx.AsyncClient(timeout=30.0) as client:
                            response = await client.get(
                                f"{settings.SEARXNG_URL}/search",
                                params={"q": fb_query, "format": "json",
                                        "categories": "general"}
                            )
                        if response.status_code == 200:
                            fb_results = response.json().get("results", [])
                            for r in fb_results:
                                url = r.get("url", "")
                                if not is_result_actually_product(r, brand, title, mpn=mpn if is_mpn_valid else None):
                                    logger.info(
                                        f"Skipping irrelevant result: {url}")
                                    continue
                                if not any(d in url.lower() for d in BLOCKED_FALLBACK) and not url.lower().endswith(".pdf"):
                                    urls.append(url)
                                    if len(urls) >= 3:
                                        break
                            urls = [
                                url for url in urls if search_service.is_likely_pdp_url(url)]
                            logger.info(
                                f"Fallback URLs after PDP filter: {urls}")
                            if urls:
                                logger.info(
                                    f"Fallback search found {len(urls)} URLs for {mpn}: {urls}")
                    except Exception as e:
                        logger.warning(
                            f"Fallback search failed for '{fb_query}': {e}")
            if cached_urls is not None and not is_algo2_run:
                cached_urls.clear()
                cached_urls.extend(urls)
                logger.info(f"Cached {len(cached_urls)} URLs for Algo 2")
            if not urls:
                return {
                    'status': 'failed',
                    'reason': 'No sources found',
                    'golden_record': {'attributes': {}}
                }
            logger.info(
                "Stage 1 completed in %.2f seconds",
                time.perf_counter() - stage1_start,
            )
            logger.info(
                f"Stage 2: Download & Extraction from {len(urls)} sources")
            stage2_start = time.perf_counter()
            # Track separate image buckets
            mfg_images_locked = []
            third_party_images = []

            # Collect all valid Manufacturer/Brand hostnames upfront
            mfg_hosts = set()
            if 'manufacturer_domain' in locals() and manufacturer_domain:
                mfg_hosts.add(urlparse(manufacturer_domain if manufacturer_domain.startswith(
                    "http") else f"https://{manufacturer_domain}").netloc.lower().replace("www.", ""))
            for d in (direct_domains if 'direct_domains' in locals() else []):
                if d:
                    mfg_hosts.add(urlparse(d if d.startswith(
                        "http") else f"https://{d}").netloc.lower().replace("www.", ""))
            all_extractions = []
            _url_semaphore = asyncio.Semaphore(3)

            async def process_url(url):
                url_start = time.perf_counter()

                extractions = []
                # nonlocal found_image_global
                # if found_image_global:
                #     logger.info(
                #         f"Image already found; skipping image extraction for {url}")
                short_description = None
                long_description = None
                features = None
                page_upc = None
                page_ean = None
                page_gtin = None
                mozu_imgs = []
                async with _url_semaphore:
                    try:
                        content = None
                        html_text = None
                        content_type = None
                        if is_algo2_run and cached_html and url in cached_html:
                            html_text = cached_html[url]
                            content_type = "html"
                            logger.info(
                                f"Algo 2: Using cached HTML for {url} - size: {len(html_text)} bytes")
                        else:
                            content = await download_service.download(url)
                            logger.info(
                                "Download for %s took %.2fs",
                                url,
                                time.perf_counter() - url_start,
                            )
                            if content is None:
                                return []
                            content_type = content.get("type")
                            if content_type == "pdf":
                                pdf_service = PDFExtractionService(
                                    max_pages=10)
                                pdf_text = await pdf_service.extract_text(content["raw_bytes"])
                                if pdf_text and len(pdf_text.strip()) > 100:
                                    pdf_lower = pdf_text.lower()
                                    if is_parts_list_pdf(pdf_text):
                                        logger.warning(
                                            f"Skipping PDF {url if 'pdf_url' not in dir() else pdf_url} — parts list/exploded view")
                                        return extractions

                                    logger.info(
                                        f"Extracted {len(pdf_text)} chars from PDF")

                                    # if is_crossref_pdf(pdf_text):
                                    if True:
                                        prod_stmt = select(Product.id).where(
                                            Product.product_code == mpn)
                                        prod_res = await db.execute(prod_stmt)
                                        prod_row = prod_res.first()
                                        prod_id = prod_row[0] if prod_row else None

                                        existing = await db.execute(
                                            select(PdfValidation).where(
                                                PdfValidation.pdf_url == url,
                                                PdfValidation.product_code == mpn,
                                            )
                                        )
                                        validation = existing.scalars().first()

                                        if not validation:
                                            validation = PdfValidation(
                                                product_code=mpn,
                                                product_id=prod_id,
                                                project_id=_UUID(
                                                    project_id) if project_id else None,
                                                pdf_url=url,
                                                source_page_url=url,
                                            )
                                            db.add(validation)
                                            await db.commit()
                                            logger.info(
                                                f"⏸ PDF needs validation, paused: {url}")
                                            return extractions

                                        if validation.status == "pending":
                                            logger.info(
                                                f"⏸ PDF validation still pending: {url}")
                                            return extractions

                                        if validation.status == "rejected":
                                            logger.info(
                                                f"✗ PDF rejected by user, skipping: {url}")
                                            return extractions

                                        logger.info(
                                            f"✓ PDF approved by user, extracting: {url}")

                                    # attrs_to_use = primary_attributes or []
                                    # if attribute_chunk:
                                    #     other_attrs = [
                                    #         a for a in attrs_to_use if a not in attribute_chunk]
                                    #     attrs_to_use = attribute_chunk + other_attrs
                                    prompt_config = _build_pdf_prompt(
                                        pdf_text=pdf_text,
                                        title=title,
                                        mpn=mpn,
                                        brand=brand,
                                        taxonomy=taxonomy,
                                        primary_attributes=primary_attributes,
                                        attribute_chunk=attribute_chunk
                                    )
                                    html_llm_start = time.perf_counter()

                                    extraction_result = await call_llm_with_schema(
                                        prompt=prompt_config["prompt"],
                                        response_model="ExtractionResponse",
                                        llm_provider=llm_provider,
                                        estimated_tokens=4000
                                    )
                                    logger.info(
                                        "[TIMING] HTML LLM (%s): %.2fs",
                                        url,
                                        time.perf_counter() - html_llm_start,
                                    )
                                    if extraction_result and extraction_result.product_detected:
                                        attr_dicts = []
                                        # image_url = None
                                        # if hasattr(extraction_result, "image_urls") :
                                        #     image_url = extraction_result.image_url
                                        source_images = list(
                                            getattr(extraction_result, "image_urls", None) or [])
                                        _merge_images(
                                            source_images, url, 'html')
                                        for attr in extraction_result.attributes:
                                            if is_distributor_metadata(attr.name):
                                                logger.info(
                                                    "Skipped distributor metadata during cached extraction: %s=%s",
                                                    attr.name,
                                                    attr.value,
                                                )
                                                continue
                                            attr_dicts.append({
                                                "name": attr.name,
                                                "value": attr.value,
                                                "unit": getattr(attr, "unit", None),
                                                "confidence": getattr(attr, "confidence", 0.95),
                                            })
                                        logger.info(
                                            f"[FINAL PAGE IMAGES] {url} => {source_images}")
                                        extractions.append({
                                            "url": url,
                                            "domain": urlparse(url).netloc,
                                            "attributes": attr_dicts,
                                            "image_assets": [
                                                {
                                                    "image_url": img,
                                                    "source_page_url": url,
                                                    "source_type": source.get('source_type', 'html')
                                                }
                                                for img in source_images
                                            ],

                                            "source_type": "pdf",
                                            "short_description": getattr(extraction_result, "short_description", None),
                                            "long_description": getattr(extraction_result, "long_description", None),
                                            "features": getattr(extraction_result, "features", None) or [],
                                            "upc": getattr(extraction_result, "upc", None),
                                            "ean": getattr(extraction_result, "ean", None),
                                            "gtin": getattr(extraction_result, "gtin", None),
                                        })
                                return extractions
                            if content_type == "html":
                                html_text = content["raw_bytes"].decode(
                                    "utf-8", errors="ignore")
                                logger.info(
                                    f"Downloaded HTML from {url} - size: {len(html_text)} bytes")
                                mozu_imgs = extract_mozu_preload_images(
                                    html_text)
                                if mozu_imgs:
                                    logger.info(
                                        f"Detected Mozu preload images ({len(mozu_imgs)}): {mozu_imgs}")
                                universal_imgs = extract_universal_product_images(
                                    html_text, url)
                                if universal_imgs:
                                    logger.info(
                                        f"[TEST] Universal Extractor found ({len(universal_imgs)}) images from {url}: {universal_imgs}")
                                    _merge_images(mozu_imgs, url, 'html')
                                if len(html_text) > 5000:
                                    from bs4 import BeautifulSoup
                                    visible_text = BeautifulSoup(
                                        html_text, 'html.parser').get_text(separator=' ', strip=True).lower()
                                    html_lower = html_text.lower()
                                    is_bot_blocked = (
                                        "just a moment..." in visible_text or
                                        "verify you are human" in visible_text or
                                        "enable javascript and cookies" in visible_text or
                                        ("cloudflare" in html_lower and "ray id:" in html_lower)
                                    )
                                    is_empty_spa = len(visible_text) < 300
                                    if is_bot_blocked:
                                        logger.info(
                                            f" [Bot Detection] Cloudflare/Security block detected on {url}. Routing to Firecrawl...")
                                    elif is_empty_spa:
                                        logger.info(
                                            f" [SPA Detection] Empty HTML shell detected ({len(visible_text)}b text). Routing to Firecrawl...")
                                    # if is_bot_blocked or is_empty_spa:
                                    #     logger.info(
                                    #         f"[SPA Detection] Large HTML ({len(html_text)}b) but almost no visible text ({len(visible_text)}b). Trying Firecrawl...")
                                    #     try:
                                    #         from app.core.config import settings
                                    #         fc_client = Firecrawl(
                                    #             api_key=settings.FIRECRAWL_API_KEY)
                                    #         fc_start = time.perf_counter()

                                    #         fc_result = await asyncio.to_thread(
                                    #             fc_client.scrape_url,
                                    #             url,
                                    #         )
                                    #         logger.info(
                                    #             "[TIMING] Firecrawl (%s): %.2fs",
                                    #             url,
                                    #             time.perf_counter() - fc_start,
                                    #         )
                                    #         fc_html = None
                                    #         if fc_result:
                                    #             if isinstance(fc_result, dict):
                                    #                 fc_html = fc_result.get(
                                    #                     'html') or fc_result.get('content')
                                    #             elif hasattr(fc_result, 'html') and fc_result.html:
                                    #                 fc_html = fc_result.html
                                    #             elif hasattr(fc_result, 'content') and fc_result.content:
                                    #                 fc_html = fc_result.content
                                    #             elif hasattr(fc_result, 'markdown') and fc_result.markdown:
                                    #                 fc_html = f"<html><body><pre>{fc_result.markdown}</pre></body></html>"
                                    #         if fc_html and len(fc_html) > 300:
                                    #             logger.info(
                                    #                 f"[SPA Detection] Firecrawl rendered content successfully - size: {len(fc_html)} bytes")
                                    #             html_text = fc_html
                                    #         else:
                                    #             logger.warning(
                                    #                 f"[SPA Detection] Firecrawl returned empty/small result. Proceeding with original HTML.")
                                    #     except Exception as fc_e:
                                    #         logger.warning(
                                    #             f"[SPA Detection] Firecrawl scrape failed: {fc_e}. Proceeding with original HTML.")
                                    if is_bot_blocked or is_empty_spa:
                                        logger.info(
                                            f"[SPA Detection] Large HTML ({len(html_text)}b) but almost no visible text "
                                            f"({len(visible_text)}b). Trying Crawl4AI..."
                                        )

                                        if getattr(settings, "USE_CRAWL4AI", True):
                                            try:
                                                ca_html = await render_with_crawl4ai(url)
                                                if ca_html and len(ca_html) > 300:
                                                    logger.info(
                                                        f"[SPA Detection] Crawl4AI rendered content successfully - size: {len(ca_html)} bytes"
                                                    )
                                                    html_text = ca_html
                                                else:
                                                    logger.warning(
                                                        "[SPA Detection] Crawl4AI returned empty/small result. "
                                                        "Proceeding with original HTML."
                                                    )
                                            except Exception as e:
                                                logger.warning(f"[SPA Detection] Crawl4AI scrape failed: {e}. "
                                                               "Proceeding with original HTML.")
                                        else:
                                            logger.info(
                                                "[SPA Detection] Crawl4AI disabled by config; skipping.")
                                if cached_html is not None and not is_algo2_run:
                                    cached_html[url] = html_text
                                    logger.info(
                                        f"Cached HTML for Algo 2: {url}")
                            else:
                                return []
                        if not html_text:
                            return []
                        if 'visible_text' not in locals() or not visible_text:
                            from bs4 import BeautifulSoup
                            visible_text = BeautifulSoup(html_text, 'html.parser').get_text(
                                separator=' ', strip=True).lower()
                        html_lower = html_text.lower()
                        not_found_phrases = [
                            "page not found",
                            "404 error",
                            "error 404",
                            "page you requested was not found",
                            "no longer available",
                            "couldn't find the page",
                            "this page doesn't exist",
                            "we're sorry, but the page"
                        ]
                        if any(phrase in visible_text for phrase in not_found_phrases):
                            matched_phrase = [
                                p for p in not_found_phrases if p in visible_text]
                            logger.warning(
                                f"Skipping {url} — Soft 404 / Page Not Found detected. Matched: {matched_phrase}")
                            return []
                        if is_mpn_valid:
                            if mpn.lower() in html_lower:
                                logger.info(
                                    f"✓ MPN verified in HTML for {url}")
                            else:
                                title_keywords = [
                                    w.lower() for w in title.split() if len(w) > 3]
                                if title_keywords:
                                    title_hits = sum(
                                        1 for kw in title_keywords if kw in html_lower)
                                    title_match_ratio = title_hits / \
                                        len(title_keywords)
                                    if brand.lower() in html_lower and title_match_ratio >= 0.6:
                                        logger.info(
                                            f"✓ Recovery: ID {mpn} missing, but Brand + Name ({int(title_match_ratio*100)}%) matched on {url}")
                                    else:
                                        logger.warning(
                                            f"Skipping {url} — Neither MPN nor strong Title match found.")
                                        return []
                                else:
                                    return []
                        elif title:
                            title_keywords = [
                                w for w in title.lower().split() if len(w) > 3]
                            if title_keywords:
                                title_hits = sum(
                                    1 for kw in title_keywords if kw in html_lower)
                                if (title_hits / len(title_keywords) >= 0.5):
                                    logger.info(
                                        f"✓ Title keywords verified in HTML for {url} (No MPN in DB)")
                                else:
                                    logger.warning(
                                        f" Skipping {url} — No MPN in DB and title match too low.")
                                    return []
                            else:
                                return []
                        has_missing_llm = missing_llm_provider and missing_llm_provider != llm_provider
                        if has_missing_llm:
                            attrs_to_use = []
                            logger.info(
                                f"Algo 1 & 2 detected - extracting all attributes for {mpn}")
                        else:
                            attrs_to_use = primary_attributes or []
                            logger.info(
                                f"Standard mode - extracting primary attributes for {mpn}")
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
                            candidate_images=candidate_images,

                            source_url=url
                        )
                        logger.info(
                            "[PROMPT] HTML size: %d bytes",
                            len(html_text.encode("utf-8"))
                        )

                        logger.info(
                            "[PROMPT] Extraction prompt size: %d chars",
                            len(prompt_config["prompt"])
                        )
                        html_llm_start = time.perf_counter()

                        extraction_result = await call_llm_with_schema(
                            prompt=prompt_config["prompt"],
                            response_model="ExtractionResponse",
                            llm_provider=llm_provider,
                            estimated_tokens=3000
                        )
                        logger.info(
                            "[TIMING] HTML LLM (%s): %.2fs",
                            url,
                            time.perf_counter() - html_llm_start,
                        )
                        logger.info(f"=== EXTRACTION RESULTS FROM {url} ===")
                        if extraction_result and extraction_result.product_detected:
                            logger.info("Product detected: YES")
                            logger.info(
                                f"Image URLs: {getattr(extraction_result, 'image_urls', None) or []}")

                            logger.info(
                                f"Number of attributes extracted: {len(extraction_result.attributes)}")
                            for attr in extraction_result.attributes:
                                logger.info(
                                    f"  - {attr.name}: {attr.value} {getattr(attr, 'unit', '')}")
                        else:
                            logger.info("Product detected: NO")
                            logger.info(
                                f"Extraction result: {extraction_result}")
                        logger.info("=====================================")
                        # attr_dicts = []
                        # image_url = None
                        # if extraction_result and extraction_result.product_detected:
                        #     if not found_image_global:
                        #         if hasattr(extraction_result, "image_urls") :
                        #             image_url = extraction_result.image_url
                        #         if not image_url:
                        #             image_url = await extract_best_image(html_text, url, mpn)
                        #         if image_url:
                        #             logger.info(
                        #                 f"✓ Image locked from {url}: {image_url}")
                        #             found_image_global = image_url
                        #     else:
                        #         image_url = None
                        # attr_dicts = []
                        # source_images: List[str] = []
                        # if extraction_result and extraction_result.product_detected:
                        #     source_images = list(getattr(extraction_result, "image_urls", None) or [])

                        #     # union structured Mozu carousel images (if present)
                        #     for u in (mozu_imgs or []):
                        #         if u not in source_images:
                        #             source_images.append(u)
                        #     source_images = source_images[:8]

                        #     if not source_images:
                        #         best = await extract_best_image(html_text, url, mpn)
                        #         if best:
                        #             source_images = [best]

                        #     _merge_images(source_images)
                        attr_dicts = []
                        source_images: List[str] = []
                        if extraction_result and extraction_result.product_detected:
                            raw_llm_imgs = list(
                                getattr(extraction_result, "image_urls", None) or [])

                            src_host = urlparse(
                                url).netloc.lower().replace("www.", "")
                            is_mfg_url = is_manufacturer_domain(
                                src_host, mfg_hosts, brand)

                            if is_mfg_url:
                                source_images = raw_llm_imgs[:]
                                for u in (mozu_imgs or []):
                                    if u not in source_images:
                                        source_images.append(u)
                                for u in (universal_imgs or []):
                                    if u not in source_images:
                                        source_images.append(u)
                                if not source_images:
                                    best = await extract_best_image(html_text, url, mpn)
                                    if best:
                                        source_images = [best]

                                source_images = source_images[:8]
                                for img in source_images:
                                    if img not in mfg_images_locked:
                                        mfg_images_locked.append(img)
                                logger.info(
                                    f"✓ Locked {len(source_images)} Manufacturer images from {url}")
                            else:
                                # THIRD-PARTY SITE (e.g. AceHardware): Skip if Manufacturer images are already locked
                                if not mfg_images_locked:
                                    source_images = raw_llm_imgs[:]
                                    for u in (mozu_imgs or []):
                                        if u not in source_images:
                                            source_images.append(u)
                                    for u in (universal_imgs or []):
                                        if u not in source_images:
                                            source_images.append(u)
                                    if not source_images:
                                        best = await extract_best_image(html_text, url, mpn)
                                        if best:
                                            source_images = [best]

                                    source_images = source_images[:8]
                                    for img in source_images:
                                        if img not in third_party_images:
                                            third_party_images.append(img)
                                else:
                                    logger.info(
                                        f"Skipped image extraction for Third-Party site ({url}) - Manufacturer images already locked.")
                            if hasattr(extraction_result, 'features'):
                                features = extraction_result.features
                            if hasattr(extraction_result, "short_description"):
                                short_description = extraction_result.short_description
                            if hasattr(extraction_result, "long_description"):
                                long_description = extraction_result.long_description
                            if hasattr(extraction_result, "upc"):
                                page_upc = extraction_result.upc
                            if hasattr(extraction_result, "ean"):
                                page_ean = extraction_result.ean
                            if hasattr(extraction_result, "gtin"):
                                page_gtin = extraction_result.gtin
                            for attr in extraction_result.attributes:
                                if is_distributor_metadata(attr.name):
                                    logger.info(
                                        "Skipped distributor metadata during cached extraction: %s=%s",
                                        attr.name,
                                        attr.value,
                                    )
                                    continue
                                attr_dicts.append({
                                    "name": attr.name,
                                    "value": attr.value,
                                    "unit": attr.unit if hasattr(attr, "unit") else None,
                                    "confidence": attr.confidence if hasattr(attr, "confidence") else 0.9
                                })
                        pdf_links = PDFExtractionService.find_pdf_links(
                            html_text, url)
                        logger.info(
                            f"find_pdf_links found {len(pdf_links)} PDF(s) on {url}")
                        # pdf_keyword_count = html_text.lower().count('.pdf')
                        # logger.info(
                        #     f"Sanity Check: The string '.pdf' appears {pdf_keyword_count} times in the raw content.")
                        # if not pdf_links:
                        #     logger.info(
                        #         f"Starting fallback PDF regex search on {len(html_text)} bytes of content.")
                        #     import re as _re
                        #     from urllib.parse import urljoin as _urljoin
                        #     html_pdfs = _re.findall(
                        #         r'href=["\']([^"\']*\.pdf(?:\?[^"\']*)?)["\']',
                        #         html_text,
                        #         _re.IGNORECASE
                        #     )
                        #     markdown_pdfs = _re.findall(
                        #         r'\]\(([^)]*\.pdf(?:\?[^)]*)?)\)',
                        #         html_text,
                        #         _re.IGNORECASE
                        #     )
                        #     bare_absolute_pdfs = _re.findall(
                        #         r'(https?://[^\s\'"<>\[\]()]+\.pdf)', html_text)
                        #     bare_relative_pdfs = _re.findall(
                        #         r'(/[^\s\'"<>\[\]()]+\.pdf)', html_text, _re.IGNORECASE)
                        #     logger.info(
                        #         f"Regex matches -> HTML: {len(html_pdfs)}, MD: {len(markdown_pdfs)}, Abs: {len(bare_absolute_pdfs)}, Rel: {len(bare_relative_pdfs)}")
                        #     pdf_hrefs = html_pdfs + markdown_pdfs + bare_absolute_pdfs + bare_relative_pdfs
                        #     logger.info(
                        #         f"Combined fallback matches: {len(pdf_hrefs)}")
                        #     for href in pdf_hrefs:
                        #         full_url = _urljoin(url, href)
                        #         if full_url not in pdf_links:
                        #             pdf_links.append(full_url)
                        #     if pdf_links:
                        #         logger.info(
                        #             f"Fallback regex found {len(pdf_links)} PDF link(s) on {url}")
                        # if pdf_links:
                        #     logger.info(
                        #         f" Found {len(pdf_links)} PDF link(s) on {url}")
                        pdf_keyword_count = html_text.lower().count('.pdf')
                        logger.info(
                            f"Sanity Check: The string '.pdf' appears {pdf_keyword_count} times in the raw content.")
                        if not pdf_links:
                            logger.info(
                                f"Starting fallback PDF regex search on {len(html_text)} bytes of content.")
                            import re as _re
                            from urllib.parse import urljoin as _urljoin
                            html_pdfs = _re.findall(
                                r'href=["\']([^"\']*\.pdf(?:\?[^"\']*)?)["\']',
                                html_text,
                                _re.IGNORECASE
                            )
                            markdown_pdfs = _re.findall(
                                r'\]\(([^)]*\.pdf(?:\?[^)]*)?)\)',
                                html_text,
                                _re.IGNORECASE
                            )
                            bare_absolute_pdfs = _re.findall(
                                r'(https?://[^\s\'"<>\[\]()]+\.pdf)', html_text)
                            bare_relative_pdfs = _re.findall(
                                r'(/[^\s\'"<>\[\]()]+\.pdf)', html_text, _re.IGNORECASE)
                            logger.info(
                                f"Regex matches -> HTML: {len(html_pdfs)}, MD: {len(markdown_pdfs)}, Abs: {len(bare_absolute_pdfs)}, Rel: {len(bare_relative_pdfs)}")
                            pdf_hrefs = html_pdfs + markdown_pdfs + bare_absolute_pdfs + bare_relative_pdfs
                            logger.info(
                                f"Combined fallback matches: {len(pdf_hrefs)}")
                            for href in pdf_hrefs:
                                full_url = _urljoin(url, href)
                                if full_url not in pdf_links:
                                    pdf_links.append(full_url)
                            if pdf_links:
                                logger.info(
                                    f"Fallback regex found {len(pdf_links)} PDF link(s) on {url}")
                        if pdf_links:
                            logger.info(
                                f" Found {len(pdf_links)} PDF link(s) on {url}")
                        for pdf_url in pdf_links or []:
                            try:
                                logger.info(f"Downloading PDF: {pdf_url}")
                                pdf_download_start = time.perf_counter()

                                pdf_content = await download_service.download(pdf_url)
                                logger.info(
                                    "[TIMING] PDF Download (%s): %.2fs",
                                    pdf_url,
                                    time.perf_counter() - pdf_download_start,
                                )
                                if pdf_content and pdf_content["type"] == "pdf":
                                    pdf_service = PDFExtractionService(
                                        max_pages=10)
                                    pdf_text = await pdf_service.extract_text(pdf_content["raw_bytes"])
                                    if pdf_text and len(pdf_text.strip()) > 100:
                                        logger.info(
                                            f"✓ Extracted {len(pdf_text)} chars from PDF")
                                        pdf_lower = pdf_text.lower()
                                        has_mpn_in_pdf = is_mpn_valid and mpn.lower() in pdf_lower
                                        if is_parts_list_pdf(pdf_text):
                                            continue
                                        has_title_in_pdf = False
                                        if not has_mpn_in_pdf and title:
                                            title_keywords = [
                                                w for w in title.lower().split() if len(w) > 3]
                                            if title_keywords:
                                                title_hits = sum(
                                                    1 for kw in title_keywords if kw in pdf_lower)
                                                has_title_in_pdf = (
                                                    title_hits / len(title_keywords) >= 0.5)
                                        if has_mpn_in_pdf:
                                            logger.info(
                                                f"✓ MPN verified in PDF for {pdf_url}")
                                        elif has_title_in_pdf:
                                            logger.info(
                                                f"✓ Title keywords verified in PDF for {pdf_url}")
                                        else:
                                            logger.warning(
                                                f" Skipping PDF {pdf_url} — neither MPN ({mpn if is_mpn_valid else 'N/A'}) "
                                                f"nor title keywords found in PDF text"
                                            )
                                            continue

                                        # attrs_to_use = primary_attributes or []
                                        # if attribute_chunk:
                                        #     other_attrs = [
                                        #         a for a in attrs_to_use if a not in attribute_chunk]
                                        #     attrs_to_use = attribute_chunk + other_attrs
                                        # if is_crossref_pdf(pdf_text):
                                        if True:
                                            existing = await db.execute(
                                                select(PdfValidation).where(
                                                    PdfValidation.pdf_url == pdf_url,
                                                    PdfValidation.product_code == mpn,
                                                )
                                            )
                                            validation = existing.scalars().first()

                                            if not validation:
                                                prod_stmt = select(Product.id).where(
                                                    Product.product_code == mpn)
                                                prod_res = await db.execute(prod_stmt)
                                                prod_row = prod_res.first()
                                                validation = PdfValidation(
                                                    product_code=mpn,
                                                    product_id=prod_row[0] if prod_row else None,
                                                    project_id=_UUID(
                                                        project_id) if project_id else None,
                                                    pdf_url=pdf_url,
                                                    source_page_url=url,
                                                )
                                                db.add(validation)
                                                await db.commit()
                                                logger.info(
                                                    f"⏸ PDF needs validation, paused: {pdf_url}")
                                                continue

                                            if validation.status == "pending":
                                                logger.info(
                                                    f"⏸ PDF validation still pending: {pdf_url}")
                                                continue

                                            if validation.status == "rejected":
                                                logger.info(
                                                    f"✗ PDF rejected by user, skipping: {pdf_url}")
                                                continue

                                            logger.info(
                                                f"✓ PDF approved by user, extracting: {pdf_url}")
                                        pdf_prompt = _build_pdf_prompt(
                                            pdf_text=pdf_text,
                                            title=title,
                                            mpn=mpn,
                                            brand=brand,
                                            taxonomy=taxonomy,
                                            primary_attributes=primary_attributes,
                                            attribute_chunk=attribute_chunk
                                        )
                                        logger.info(
                                            "[PROMPT] PDF text size: %d chars",
                                            len(pdf_text)
                                        )

                                        logger.info(
                                            "[PROMPT] PDF prompt size: %d chars",
                                            len(pdf_prompt["prompt"])
                                        )
                                        pdf_llm_start = time.perf_counter()

                                        pdf_result = await call_llm_with_schema(
                                            prompt=pdf_prompt["prompt"],
                                            response_model="ExtractionResponse",
                                            llm_provider=llm_provider,
                                            estimated_tokens=4000
                                        )
                                        logger.info(
                                            "[TIMING] PDF LLM (%s): %.2fs",
                                            pdf_url,
                                            time.perf_counter() - pdf_llm_start,
                                        )
                                        if pdf_result and pdf_result.product_detected:
                                            pdf_attrs = []
                                            for attr in pdf_result.attributes:
                                                if is_distributor_metadata(attr.name):
                                                    logger.info(
                                                        "Skipped distributor metadata during PDF extraction: %s=%s",
                                                        attr.name,
                                                        attr.value,
                                                    )
                                                    continue
                                                pdf_attrs.append({
                                                    "name": attr.name,
                                                    "value": attr.value,
                                                    "unit": getattr(attr, "unit", None),
                                                    "confidence": getattr(attr, "confidence", 0.95)
                                                })
                                            pdf_images = list(
                                                getattr(pdf_result, "image_urls", None) or [])
                                            _merge_images(
                                                pdf_images, pdf_url, 'pdf')

                                            extractions.append({
                                                "url": pdf_url,
                                                "domain": urlparse(pdf_url).netloc,
                                                "attributes": pdf_attrs,
                                                 "image_assets": [
                                                    {
                                                        "image_url": img,
                                                        "source_page_url": pdf_url,
                                                        "source_type": "pdf"
                                                    }
                                                    for img in (list(getattr(pdf_result, "image_urls", None) or []))
                                                ],
                                                "source_type": "pdf",
                                                "short_description": getattr(pdf_result, "short_description", None),
                                                "long_description": getattr(pdf_result, "long_description", None),
                                                "features": getattr(pdf_result, "features", None) or [],
                                                "upc": getattr(pdf_result, "upc", None),
                                                "ean": getattr(pdf_result, "ean", None),
                                                "gtin": getattr(pdf_result, "gtin", None),
                                            })
                                            logger.info(
                                                f"✓ PDF extraction: {len(pdf_attrs)} attributes from {pdf_url}")
                                            for attr in pdf_attrs:
                                                logger.info(
                                                    f"  - {attr['name']}: {attr['value']} {attr.get('unit') or ''}")
                            except Exception as pdf_err:
                                logger.warning(
                                    f"Failed to process PDF {pdf_url}: {pdf_err}")
                                continue

                        extractions.append({
                            "url": url,
                            "domain": urlparse(url).netloc,
                            "attributes": attr_dicts,
                            "image_assets": [
                                {
                                    "image_url": img,
                                    "source_page_url": url,
                                    "source_type": content_type if content_type in ['html', 'pdf'] else 'html'
                                }
                                for img in source_images
                            ],


                            "source_type": "html",
                            "short_description": short_description,
                            "long_description": long_description,
                            "features": features,
                            "upc": page_upc,
                            "ean": page_ean,
                            "gtin": page_gtin,
                        })
                        return extractions
                    except Exception as e:
                        logger.warning(f"Extraction failed for {url}: {e}")
                        return []
            tasks = [process_url(url) for url in urls[:5]]
            results = await asyncio.gather(*tasks)
            all_extractions = [e for sub in results if sub for e in sub]
            if not all_extractions:
                return {
                    'status': 'failed',
                    'reason': 'No valid extractions',
                    'golden_record': {'attributes': {}}
                }
            html_attrs = sum(len(s['attributes']) for s in all_extractions if s.get(
                'source_type') == 'html')
            pdf_attrs = sum(len(s['attributes'])
                            for s in all_extractions if s.get('source_type') == 'pdf')
            total_attrs = html_attrs + pdf_attrs
            logger.info(
                f"Stage 2 extracted {total_attrs} total attributes "
                f"({html_attrs} from HTML, {pdf_attrs} from {sum(1 for s in all_extractions if s.get('source_type') == 'pdf')} PDF(s))"
            )
        total_extracted_attrs = sum(len(s.get('attributes', []))
                                    for s in all_extractions)
        if total_extracted_attrs == 0:
            logger.warning(
                f"No attributes extracted for {mpn}. Aborting to prevent LLM hallucination.")
            return {
                'status': 'failed',
                'reason': 'Product not found or no attributes could be extracted.',
                'golden_record': {'attributes': {}}
            }
        logger.info(
            "Stage 2 completed in %.2f seconds",
            time.perf_counter() - stage2_start,
        )
        logger.info("Stage 3: Combined Cleaning, Unification & Standardization")
        stage3_start = time.perf_counter()

        algo_name_map = {
            'openai': 'Algo 1',
            'gemini': 'Algo 2',
            'claude': 'Algo 3'
        }
        current_algo_name = algo_name_map.get(llm_provider, llm_provider)
        raw_attrs_for_combine = []
        for src_idx, source in enumerate(all_extractions):
            source_type = source.get('source_type', 'html')
            for attr in source['attributes']:
                attr_name = attr.get("name", "")
                if is_distributor_metadata(attr_name):
                    logger.info(
                        "Filtered distributor metadata before aggregation: %s=%s",
                        attr_name,
                        attr.get("value"),
                    )
                    continue
                raw_attrs_for_combine.append({
                    'temp_id': f"{src_idx}_{len(raw_attrs_for_combine)}",
                    'name': attr['name'],
                    'value': attr['value'],
                    'unit': attr.get('unit'),
                    'source_url': source['url'],
                    'confidence': attr.get('confidence', 0.9),
                    'extraction_algorithm': current_algo_name,
                    'extraction_source': source_type
                })
        raw_attrs_for_combine = normalize_concatenated_uom(
            raw_attrs_for_combine)
        canonical_names = []
        canonical_units = {}
        alias_name_map = {}
        category = None
        if db and taxonomy:
            tax_parts = [p.strip() for p in taxonomy.split(" > ") if p.strip()]
            cat_stmt = select(Category).where(
                or_(
                    Category.full_path == taxonomy,
                    Category.name == tax_parts[-1]
                )
            )
            cat_result = await db.execute(cat_stmt)
            category = cat_result.scalars().first()
            if category:
                try:
                    asyncio.create_task(
                        _enqueue_alias_job_isolated(category.id))
                except Exception as e:
                    logger.warning(
                        f"[Stage3Canonicals] Failed to enqueue alias job: {e}")
                try:
                    winner_attrs, alias_name_map = await load_category_canonical_winners(category.id, db)
                except Exception as e:
                    logger.warning(
                        f"[Stage3Canonicals] resolver failed for category_id={category.id}: {e}")
                    winner_attrs, alias_name_map = [], {}
                canonical_names = [
                    a.attribute_name for a in winner_attrs if a.attribute_name]
                canonical_units = {
                    a.attribute_name: a.unit for a in winner_attrs if a.attribute_name and a.unit}
                alias_lc = {k.lower(): v for k, v in alias_name_map.items()}
                for a in raw_attrs_for_combine:
                    n = a.get("name") or ""
                    mapped = alias_lc.get(n.lower())
                    if mapped:
                        a["name"] = mapped
                logger.info(
                    f"[Stage3Canonicals] category_id={category.id} taxonomy='{taxonomy}' "
                    f"winners={len(canonical_names)} aliases={len(alias_name_map)} units={len(canonical_units)}"
                )
                logger.info(
                    f"[Stage3Canonicals] winners_sample={canonical_names[:80]}")
                if alias_name_map:
                    items = list(alias_name_map.items())
                    logger.info(
                        f"[Stage3Canonicals] alias_sample={items[:40]}")
                logger.info(
                    f"[Stage3Canonicals] canonical_units_count={len(canonical_units)}")
                logger.info(
                    f"[Stage3Canonicals] canonical_units_sample={dict(list(canonical_units.items())[:80])}")
                logger.info(
                    f"Loaded {len(canonical_names)} canonical names, {len(canonical_units)} units for: {taxonomy}")
        logger.info("Stage 2.5: Semantic Attribute Clustering")
        logger.info(f"category_id passed: {category.id if category else None}")
        cluster_start = time.perf_counter()
        raw_attrs_for_combine = await cluster_attributes_by_meaning(
            raw_attrs_for_combine,
            category_id=category.id if category else None,
            db=db,
            canonical_names=canonical_names,
            threshold=0.85
        )
        logger.info(
            "Semantic clustering took %.2fs",
            time.perf_counter() - cluster_start,
        )
        if category and alias_name_map:
            alias_lc = {k.lower(): v for k, v in alias_name_map.items()}
            for a in raw_attrs_for_combine:
                n = (a.get("name") or "").lower()
                mapped = alias_lc.get(n)
                if mapped:
                    a["name"] = mapped
        logger.info(
            f"After clustering: {len(set(a['name'] for a in raw_attrs_for_combine))} unique names")
        project = await db.get(Project, project_id) if db and project_id else None
        use_case = project.use_case.lower() if project and project.use_case else ""
        combine_prompt = _build_combined_prompt(
            raw_attrs_for_combine, brand, mpn, title, taxonomy,
            existing_excel_attrs=existing_excel_attrs, use_case=use_case,
            canonical_names=canonical_names, canonical_units=canonical_units)
        logger.info(
            "[PROMPT] Combine prompt size: %d chars",
            len(combine_prompt)
        )
        async for attempt in AsyncRetrying(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10)):
            with attempt:
                combine_start = time.perf_counter()

                combined_result = await call_llm_with_schema(
                    prompt=combine_prompt,
                    response_model="UnifiedStandardizedResponse",
                    llm_provider=llm_provider,
                    estimated_tokens=3000 + len(raw_attrs_for_combine) * 200,
                    max_tokens=min(
                        3000 + len(raw_attrs_for_combine) * 200, 16000)
                )
                logger.info(
                    "[TIMING] Combine LLM: %.2fs",
                    time.perf_counter() - combine_start,
                )
        golden_attributes = combined_result.attributes
        forbidden_names = {
            'brand', 'sku', 'mpn', 'part number', 'manufacturer part number',
            'model number', 'item number', 'upc', 'gtin', 'manufacturer part id'
        }
        filtered_attributes = []
        for attr in golden_attributes:
            attr_name = (getattr(attr, "name", None) or "").strip()
            attr_name_lower = attr_name.lower()

            if is_distributor_metadata(attr_name):
                logger.info(
                    "Filtered distributor metadata after aggregation: %s",
                    attr_name,
                )
                continue

            if any(
                forbidden in attr_name_lower
                for forbidden in forbidden_names
            ):
                logger.info(
                    "Filtered forbidden identifier attribute: %s",
                    attr_name,
                )
                continue

            filtered_attributes.append(attr)

        golden_attributes = filtered_attributes
        golden_attr_dicts_temp = [
            {'name': a.name, 'value': a.value, 'unit': a.unit}
            for a in golden_attributes
        ]
        golden_attr_dicts_temp = _standardize_uom_in_attrs(
            golden_attr_dicts_temp)
        for attr, standardized in zip(golden_attributes, golden_attr_dicts_temp):
            if attr.unit != standardized['unit']:
                logger.info(
                    f"UOM standardized: '{attr.unit}' → '{standardized['unit']}' "
                    f"for '{attr.name}'"
                )
                attr.unit = standardized['unit']
        input_names = {(a['name'] or "").strip().lower()
                       for a in raw_attrs_for_combine}
        output_names = {(a.name or "").strip().lower()
                        for a in golden_attributes}
        merged_names = {
            (m or "").strip().lower()
            for a in golden_attributes
            for m in (a.merged_from or [])
        }
        accounted_for = output_names | merged_names
        missing = input_names - accounted_for
        extra = output_names - input_names
        logger.info(f"LLM input attribute count: {len(input_names)}")
        logger.info(f"LLM output attribute count: {len(output_names)}")
        if merged_names:
            logger.info(
                f"LLM merged (visible) attribute count: {len(merged_names)}")
        if missing:
            dropped_with_values = {a['name']: a['value'] for a in raw_attrs_for_combine if (
                a['name'] or "").strip().lower() in missing}
            logger.warning(
                f"⚠ LLM SILENTLY DROPPED attributes (not merged, not output): {dropped_with_values}")
        if extra:
            logger.warning(f"⚠ LLM CREATED new attributes: {extra}")
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
            validation_start = time.perf_counter()

            validation_result = await call_llm_with_schema(
                prompt=validation_config['prompt'],
                response_model="ValidationResponse",
                llm_provider=missing_llm_provider,
                estimated_tokens=1500
            )
            logger.info(
                "[TIMING] Validation LLM: %.2fs",
                time.perf_counter() - validation_start,
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
        logger.info("Stage 3 completed in %.2f seconds",
                    time.perf_counter() - stage3_start,)
        stage5_start = time.perf_counter()

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
                'sources': a.sources,
                'extraction_algorithm': getattr(a, 'extraction_algorithm', current_algo_name),
                'extraction_source': getattr(a, 'extraction_source', 'html')
            }
            for a in golden_attributes
        ]
        best_short_description = None
        best_long_description = None
        best_upc = None
        best_ean = None
        best_gtin = None
        all_features = []
        seen_features = set()
        for source in all_extractions:
            if source.get('short_description') and not best_short_description:
                best_short_description = source['short_description']
            if source.get('long_description') and not best_long_description:
                best_long_description = source['long_description']
            if source.get('upc') and not best_upc:
                best_upc = source['upc']
            if source.get('ean') and not best_ean:
                best_ean = source['ean']
            if source.get('gtin') and not best_gtin:
                best_gtin = source['gtin']
            source_features = source.get('features') or []
            for feat in source_features:
                if source_features:
                    logger.info(f"[FEATURE SOURCE DEBUG] {source['url']} contributed features: {source_features}")
                if feat and feat.strip() and feat not in seen_features:
                    all_features.append(feat)
                    seen_features.add(feat)
            if best_short_description and best_long_description:
                break
        logger.info("Stage 6: Marketing Enrichment")
        logger.info(
            f"[DEBUG FEATURES] all_features going into enrichment: {all_features}")
        enrichment_config = build_enrichment_prompt(
            golden_attributes=golden_attr_dicts,
            product_name=title,
            brand=brand or "",
            taxonomy=taxonomy or "",
            existing_short_description=best_short_description,
            existing_long_description=best_long_description,
            existing_features=all_features
        )
        logger.info(
            "[PROMPT] Enrichment prompt size: %d chars",
            len(enrichment_config["prompt"])
        )
        if is_algo2_run:
            logger.info(f"Algo2 :Skipping enrichment (router will do it )")
            enrichment_result = type('obj', (object,), {
                'short_description': '',
                'long_description': '',
                'features': []
            })()
        else:
            enrichment_start = time.perf_counter()

            enrichment_result = await call_llm_with_schema(
                prompt=enrichment_config['prompt'],
                response_model="EnrichmentResponse",
                llm_provider=missing_llm_provider,
                estimated_tokens=2000,
                max_tokens=4000
            )
            logger.info(
                "[TIMING] Marketing Enrichment: %.2fs",
                time.perf_counter() - enrichment_start,
            )
        # # --- NEW: Final Priority Decision ---
        # if mfg_images_locked:
        #     logger.info(
        #         f"✓ Final Decision: Using {len(mfg_images_locked)} Manufacturer image(s) EXCLUSIVELY")
        #     found_image_assets_global = mfg_images_locked[:8]
        # elif third_party_images:
        #     logger.info(
        #         f"⚠️ Final Decision: No manufacturer images found. Using {len(third_party_images)} third-party fallback image(s)")
        #     found_image_assets_global = third_party_images[:8]
        # else:
        #     pass
                # --- NEW: Final Priority Decision (Preserves Dict Structure) ---
        prioritized_assets = []
        
        # 1. Prioritize Manufacturer images
        for asset in found_image_assets_global:
            if asset['image_url'] in mfg_images_locked:
                prioritized_assets.append(asset)
                
        # 2. Fallback to Third-Party images if no manufacturer images exist
        if not prioritized_assets:
            for asset in found_image_assets_global:
                if asset['image_url'] in third_party_images:
                    prioritized_assets.append(asset)
                    
        # 3. If still empty, keep whatever we have
        if not prioritized_assets:
            prioritized_assets = found_image_assets_global

        found_image_assets_global = prioritized_assets[:8]

        best_image = found_image_assets_global[0] if found_image_assets_global else None

        if not best_image:
            best_image = extract_best_image_fallback(all_extractions)
            if best_image:
                if isinstance(best_image, str):
                    best_image_dict = {
                        "image_url": best_image,
                        "source_page_url": urls[0] if urls else "unknown_source",
                        "source_type": "fallback",
                        "is_primary": True
                    }
                    if best_image not in [a.get("image_url") for a in found_image_assets_global]:
                        found_image_assets_global.append(best_image_dict)
                elif isinstance(best_image, dict) and best_image not in found_image_assets_global:
                    found_image_assets_global.append(best_image)
            
            for candidate in candidate_images:
                is_valid = await validate_image_url(candidate)
                if is_valid:
                    logger.info(f"Fallback to SearXNG image: {candidate}")
                    best_image = candidate
                    if candidate not in [a.get("image_url") for a in found_image_assets_global]:
                        found_image_assets_global.append({
                            "image_url": candidate,
                            "source_page_url": urls[0] if urls else "unknown_source",
                            "source_type": "image_search",
                            "is_primary": True
                        })
                    break
        if 'download_service' in locals():
            download_service._cache.clear()
        if cached_html is not None:
            cached_html.clear()
            logger.info("Cleared cached HTML for fresh extraction")
        final_features = []
        if hasattr(enrichment_result, 'features') and enrichment_result.features:
            final_features = list(enrichment_result.features)
        elif all_features:
            final_features = all_features
        logger.info(
            f"[DEBUG FEATURES] final_features after enrichment: {final_features}")
        seen_final = set()
        deduped_features = []
        for f in final_features:
            if f and f.strip() and f not in seen_final:
                deduped_features.append(f)
                seen_final.add(f)
        logger.info(
            "Stage 5+6 completed in %.2f seconds",
            time.perf_counter() - stage5_start,
        )

        logger.info(
            "TOTAL aggregation time: %.2f seconds",
            time.perf_counter() - pipeline_start,
        )
        return {
            'status': 'success',
            'golden_record': {
                'attributes': {attr['name']: attr for attr in golden_attr_dicts},
                'short_description': enrichment_result.short_description or "",
                'long_description': enrichment_result.long_description,
                'features': deduped_features,
                'sources_consulted': list({s['url'] for s in all_extractions}),
                'confidence': avg_conf,
                'upc': best_upc or "",
                'ean': best_ean or "",
                'gtin': best_gtin or "",
            },
            'validation_conflicts': validation_conflicts,
            'excel_overrides': excel_overrides,
            'image_assets': found_image_assets_global, 
            'image_url': found_image_assets_global[0]['image_url'] if found_image_assets_global else None,
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
    use_case: str = None,
    canonical_names: List[str] = None,
    canonical_units: Dict[str, str] = None
) -> str:
    attr_lines = []
    for a in raw_attrs:
        line = f"ID: {a['temp_id']}\n  Name: {a['name']}\n  Value: {a['value']}"
        if a.get('unit'):
            line += f"\n  Unit: {a['unit']}"
        line += f"\n  Confidence: {a.get('confidence', 0.9)}"
        if a.get('source_url'):
            line += f"\n  Source: {a['source_url']}"
        if a.get('extraction_algorithm'):
            line += f"\n  Algorithm: {a['extraction_algorithm']}"
        if a.get('extraction_source'):
            line += f"\n  Source Type: {a['extraction_source']}"
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
    
    canonical_section = ""
    if canonical_names:
        names_list = "\n".join(f"  - {n}" for n in canonical_names)
        canonical_section = f"""
    ═══════════════════════════════════════════════════════
    PREFERRED CANONICAL ATTRIBUTE NAMES  ★ HIGHEST PRIORITY ★
    ═══════════════════════════════════════════════════════
    You are a Senior Product Data Standardization Expert with deep knowledge of
    industrial, electrical, mechanical, and consumer product taxonomies.
    Your job is NOT just to match strings — it is to understand MEANING.
    You must think like a domain expert who knows that two differently-worded
    attributes can represent the exact same physical property.
    CORE INTELLIGENCE RULES:
    1. ABBREVIATION EXPANSION: Treat abbreviated and full forms as identical.
    "Max" = "Maximum", "Min" = "Minimum", "Temp" = "Temperature",
    "Vol" = "Volume", "Wt" = "Weight", "Qty" = "Quantity", "Dia" = "Diameter"
    Apply this logic to ALL attributes, not just these examples.
    2. WORD ORDER VARIATION: Same words in different order = same attribute.
    "Air Flow Maximum" = "Maximum Air Flow" = "Max Air Flow"
    "Operating Pressure Maximum" = "Maximum Operating Pressure"
    Apply this logic universally — do not look for exact word order.
    3. SYMBOL/WORD EQUIVALENCE: Symbols and their word equivalents are identical.
    "w/" = "with", "w/o" = "without", "&" = "and"
    "Weight (w/ Battery)" = "Weight (with Battery)"
    4. SYNONYM RESOLUTION: Understand domain synonyms as the same concept.
    "Air Volume" = "Air Flow", "Roll Count" = "Number of Rolls"
    Think beyond the examples given — apply semantic understanding broadly.
    5. SPECIFICITY RULE: If the raw attribute is MORE specific than a preferred
    name, preserve the raw name (more detail is better than less).
    6. DISCOVERY MANDATE (HIGHEST PRIORITY):
    You are in 'Discovery Mode' because the preferred list is short or empty.
    - DO NOT limit the output to the 'Preferred' list.
    - You MUST return EVERY technical specification found in the input.
    - If you found 60 attributes in the raw data, I expect 60 attributes in your JSON.
    - If you return only a few attributes, you have FAILED the task.
    - Capture everything: technical specs, dimensions, material, electrical data, performance metrics.
    PREFERRED NAMES FOR THIS TAXONOMY:
    {names_list}
    FINAL INSTRUCTION: For every raw attribute you process, ask yourself:
    "Does this mean the same thing as any preferred name, even if worded differently?"
    If YES → use the preferred name. If NO → create a clean new name.
    ═══════════════════════════════════════════════════════
    """
    if canonical_units:
        units_list = "\n".join(
            f"  - {name}: {unit}" for name, unit in canonical_units.items())
        canonical_section += f"""
    ═══════════════════════════════════════════════════════
    PREFERRED UNITS FOR THIS TAXONOMY (use EXACTLY these units):
    ═══════════════════════════════════════════════════════
    {units_list}
    If a raw attribute matches a preferred name, its unit MUST match
    the preferred unit shown here — regardless of how the source wrote it.
    ═══════════════════════════════════════════════════════
    """
    all_unique_names = sorted(set(a['name'] for a in raw_attrs))
    names_checklist = "\n".join(f"  ☐ {n}" for n in all_unique_names)
    mandatory_section = f"""
    ═══════════════════════════════════════════════════════
    MANDATORY ATTRIBUTES CHECKLIST  ★ DO NOT SKIP ANY ★
    ═══════════════════════════════════════════════════════
    The following {len(all_unique_names)} unique attribute names were found
    across all sources. You MUST include EVERY one in your output.
    After producing your JSON, count your output attributes.
    If you have fewer than {len(all_unique_names)}, you have FAILED.
    {names_checklist}
    ═══════════════════════════════════════════════════════
    """
    prompt = f"""
You are a Senior Product Data Engineer. Process the raw product attributes below.
Your job: clean → unify synonyms → standardize → return ONE canonical attribute per concept.
{canonical_section}
{mandatory_section} 
STRICT DATA RETENTION MANDATE:
    1. If an attribute like 'AMPS' or 'Voltage' is in the input, it MUST be in the output.
    2. If the name is in the 'PREFERRED' list, RENAME it (e.g., 'AMPS' -> 'Amperage').
    3. If the name is NOT in the list, keep the original name.
     4. NEVER delete technical data. If you drop an attribute that has a value, it is a CRITICAL FAILURE.
    EXCEPTION: identifiers that restate the product's own MPN/SKU/Brand
    (see RULE 15 below) are not technical data — they are metadata already
    known from context, and dropping them is required, not a failure.
================================================================
RULE 0 — FORBIDDEN ATTRIBUTES (HIGHEST PRIORITY)
================================================================
You MUST NOT output any of the following as attributes. These are already known from product context:
- Brand
- SKU
- MPN
- Part Number
- Manufacturer Part Number
- Model Number
- Item Number
- UPC
- GTIN
- Any attribute name containing "Part Number", "Model Number", "Manufacturer Part", "SKU", "MPN", or "Brand"
If any raw attribute matches the above names, **drop it completely**. Do not include it in the final attributes list.
================================================================
PRODUCT CONTEXT:
  MPN: {mpn}
  Brand: {brand}
  Title: {title}
  Taxonomy: {taxonomy or 'General'}
    ═══════════════════════════════════════════════════════
RULE 15 — IDENTIFIER SUPPRESSION  ★ APPLIES BEFORE ANY OTHER RULE ★
  If any attribute's value IS the MPN, CONTAINS the MPN, or is a brand+MPN
  combination (e.g. "Ninja FB151BL" when MPN is "FB151BL"), under any
  attribute name (Model Name, Model Number, Item Model Number, Part Number,
  Manufacturer Part Number, etc.), drop that attribute entirely. This is
  the one case where dropping an attribute with a value is correct, not
  a failure — see the exception noted in the Data Retention Mandate above.
RULE 16 — SEMANTIC EQUIVALENCE (GENERAL CASE)
  Two attributes are the same spec if they measure the same real-world
  property, regardless of how differently they are named, worded, or
  formatted — including cases not covered by any rule or example above.
  Judge by meaning, not by string similarity. When in doubt, ask: would
  a knowledgeable buyer treat these as one fact or two? If one fact,
  merge them, keeping the version with a unit and the clearer name.
RULE 17 — NAME COLLISION
  If two attributes describe genuinely different facts but would naturally
  share the same short name, give them distinct, more specific names
  instead of reusing the same name, e.g. "Capacity (Volume)" vs
  "Capacity (Can Count)" rather than two attributes both named "Capacity".
RULE 18 — MERGE SAFETY CHECK  ★ APPLIES BEFORE RULE 16 ★
  Before merging any two attributes under Rule 16, verify ALL of these:
  a) They measure the exact same underlying quantity, not merely a related
     or nearby one. Shared vocabulary or overlapping topic is not sufficient.
  b) They represent the same shape of data — a range, a single value, a
     count, and a rate are all different shapes; do not collapse one into
     another even if they describe the same general property.
  c) No information would be lost. If the two values are not fully
     recoverable from a single merged result, they are not the same fact.
  If any of a/b/c fails, keep both attributes separate with distinct names,
  even if Rule 16's general similarity logic would otherwise suggest merging.
  When uncertain, do not merge — keeping two similar attributes separate is
  always safer than incorrectly discarding real data.
RULE 19 — MERGE TRANSPARENCY  ★ MANDATORY ★
  If this output attribute absorbed one or more OTHER differently-named raw
  attributes (per Rule 16), list every absorbed raw name in "merged_from".
  If no merge occurred (this attribute maps 1:1 from a single raw name),
  set "merged_from" to an empty list [].
  This field must account for every raw attribute name that does not appear
  as its own output attribute — if a raw name is missing from BOTH the
  output attributes AND every merged_from list, that is a bug you must avoid.
  ═══════════════════════════════════════════════════════
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
RULE 1.5 — COMPOUND MEASUREMENTS ★ HIGHEST PRIORITY ★
  Values with TWO different units must be preserved with both units separated by comma.
  "1 ft. 2 in."    → value: "1, 2",    unit: "ft, in"
  "2 ft. 5 in."    → value: "2, 5",    unit: "ft, in"
  "8 ft. 8 in."    → value: "8, 8",    unit: "ft, in"
  "2 m 30 cm"      → value: "2, 30",   unit: "m, cm"
  "3/8 x 15 ft"    → value: "0.375, 15", unit: "in, ft"
  DO NOT convert between units. DO NOT isolate a single unit.
  Extract both numbers and both units, separated by comma.
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
  in. → "in"
  yd. → "yd"
  ft x in → "ft x in"
  in x ft → "in x ft"
  in x in → "in x in"
  ft x ft → "ft x ft"
RULE 12.1 — UOM STANDARDIZATION  ★ APPLY TO `unit` FIELD ONLY ★
  After extracting the unit, standardize it using these EXACT mappings:
  ┌─────────────────────────────────────────────────────────────┐
  │ INPUT VARIATIONS              →  STANDARDIZED OUTPUT       │
  ├─────────────────────────────────────────────────────────────┤
  │ inch, inches, in.             →  in                         │
  │ foot, feet, ft, ft.           →  ft                         │
  │ yard, yd, yd.                 →  yd                         │
  │ millimeter, mm                →  mm                         │
  │ centimeter, cm                →  cm                         │
  │ gallon, gal, gal.             →  gal                        │
  │ pound, lb, lbs, lb.           →  lb                         │
  │ volt, volts, v, V             →  V                          │
  │ amp, amps, A                  →  A                          │
  │ watt, watts, w, W             →  W                          │
  │ tpi, TPI                      →  TPI                        │
  │ mil                           →  mil                        │
  │ rpm, RPM                      →  RPM                        │
  │ deg c, celsius, °c            →  deg C                      │
  │ deg f, fahrenheit, °f         →  deg F                      │
  └─────────────────────────────────────────────────────────────┘
  CRITICAL:
  - Apply this to the `unit` field, NEVER to the `value` field
  - Preserve compound units: "ft, in", "in x ft", "ft x in"
  - Case matters: "V" not "v", "TPI" not "tpi", "mm" not "MM"
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
  ✓ Drop "Manufacturer Address", "Company Address", "Contact Info" — these are contact details, not product specifications.
    ✓ Drop "Marketing Language", "Language", "Supported Languages" — these are website metadata, not product specifications.
    ✓ Brand, MPN, SKU are already in product context — no need to extract them as separate attributes.
  (e.g., "CCT" and "Color Temperature", "Colour" and "Color").
  Produce ONE output attribute per concept using the most canonical name.
  Pick the value with the highest confidence; list all source URLs and original values.
  ★ IMPORTANT ★ Specific synonym groups:
    - Temperature attributes: treat "Maximum Operating Temperature" and "Operating Temperature - Maximum" as the same.
      If both appear, keep the one that matches the primary attributes list; otherwise keep the one with higher confidence.
    - Material attributes: if both "Material" and "Backing Material" refer to the same material, keep only the one that matches the primary list.
      If neither is in the primary list, keep "Backing Material" (more specific).
✓ Are dB and dB(A) mixed? If both present with same value → keep dB(A)
  ✓ Is MPN duplicated under "Model Number", "Part Number", or "Model 
  ✓ Do any "Number of X" and "X Count" pairs exist with same value? → merge to one
  ✓ Do any attributes differ ONLY in casing (e.g., "Spraying Capacity per Charge" vs "Spraying Capacity Per Charge")? → merge to one, keep the one with unit
✓ If one has a unit and the other doesn't, keep the one WITH the unit (more complete)
✓ COMPOUND vs SINGLE: If one attribute has "x" values (e.g., "0.375 x 15") and another has just a number that matches part of it (e.g., "15"), keep the compound one, drop the single
  ✓ Are VDC/VAC casing normalized to standard form (not flattened to V)?
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
Input:  Name="Spraying Capacity per Charge", Value="80", unit="gallons"
        Name="Spraying Capacity Per Charge", Value="80", unit=null
Output: name="Spraying Capacity Per Charge", value="80", unit="gallons"
        (kept the one with unit, merged)
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
Input:  Name="Hose",               Value="0.375 x 15"
Output: name="Hose", value="0.375 x 15", unit="in x ft"
Input:  Name="Hose Length",        Value="0.375 x 15"
Output: name="Hose", value="0.375 x 15", unit="in x ft"
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
      "original_values": ["original raw value"],
       "original_values": ["original raw value"],
      "merged_from": ["Other Raw Attribute Name That Was Folded In"]
    }}
  ],
  "summary": "Brief explanation of major changes and grouping decisions."
}}
CRITICAL FINAL CHECKS before returning:
   ✓ Does any value still contain a SINGLE unit string? If yes → fix it (EXCEPTION: compound imperial per RULE 1.5 — "ft, in" format is correct).
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
  ✓ Does any output attribute's value restate the product's own MPN (alone or combined with Brand, e.g. "Brand MPN")? If yes → remove it per RULE 15.
  ✓ Do any two output attributes share the same name but describe different facts? If yes → rename per RULE 17 to make them distinct.
  ✓ UOM STANDARDIZATION CHECK:
  - "in" not "inches"/"in."/"Inch"
  - "ft" not "feet"/"ft."/"Foot"  
  - "gal" not "gallon"/"Gal"/"gallons"
  - "V" not "volts"/"v"/"Volt"
  - "TPI" not "tpi"
  - "mm" not "Millimeter"/"MM"
  If any unit is non-standard → fix it per RULE 12.1 mapping.
{excel_section}
{validation_section}
═══════════════════════════════════════════════════════
INPUT ATTRIBUTES
═══════════════════════════════════════════════════════
{attributes_text}
═══════════════════════════════════════════════════════
        CRITICAL: PRESERVE EXTRACTION METADATA
    ═══════════════════════════════════════════════════════
    Each attribute includes:
    - Algorithm: Which LLM extraction (Algo 1, Algo 2, or Algo 3)
    - Source Type: html or pdf
    When consolidating duplicates:
    1. Keep metadata from HIGHER PRIORITY source:
       - Gap-filling algorithms (Algo 2, Algo 3) are HIGHER priority than base (Algo 1) because they are more targeted.
       - pdf is HIGHER priority than html (datasheets are more authoritative).
    2. Return in output: extraction_algorithm, extraction_source
    Example:
    Input: "Voltage" (Algo 1, html) + "Voltage Rating" (Algo 2, html)
    Output: "Voltage Rating" (Algo 2, html)
    Input: "Voltage" (Algo 1, html) + "Voltage Rating" (Algo 3, pdf)
    Output: "Voltage Rating" (Algo 3, pdf)
"""
    return prompt
