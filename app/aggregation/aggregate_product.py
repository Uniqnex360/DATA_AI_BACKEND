from typing import Dict, List, Optional

from sqlalchemy import func
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
# before category and brand prompt
# async def aggregate_product(
#     mpn: str,
#     title: str,
#     sku: Optional[str] = None,
#     upc: Optional[str] = None,
#     brand: Optional[str] = None,
#     taxonomy: Optional[str] = None,
#     primary_attributes: Optional[List[str]] = None,
#     db: Optional[AsyncSession] = None,
#     project_id: str = None,
#     llm_provider: str = 'openai',
#     attribute_chunk: Optional[List[str]] = None,
#     existing_excel_attrs: Optional[Dict[str, str]] = None
# ) -> Dict:
#     try:
#         logger.info(f"Starting aggregation for {mpn}")
#         logger.info("Stage 1: URL Discovery")
#         search_service = SmartSearchService(llm_provider,db=db, max_results=5)
#         query = title if (
#             mpn in title and brand in title) else f"{brand} {mpn} {title}"
#         query = query.strip()
#         urls, candidate_images = await search_service.get_urls(
#             query, mpn=mpn, brand=brand, title=title, sku=sku
#         )
#         if not urls:
#             return {
#                 'status': 'failed',
#                 'reason': 'No sources found',
#                 'golden_record': {'attributes': {}}
#             }
#         logger.info(f"Stage 2: Download & Extraction from {len(urls)} sources")
#         download_service = HttpDownloadService(
#             timeout=30,
#         )
#         all_extractions = []
#         _url_semaphore = asyncio.Semaphore(2)
#         async def process_url(url):
#             async with _url_semaphore:
#                 try:
#                     content = await download_service.download(url)
#                     if content is None:
#                         return None
#                     if content['type'] == 'html':
#                         html_text = content['raw_bytes'].decode(
#                             'utf-8', errors='ignore')
#                         logger.info(
#                             f"Downloaded HTML from {url} - size: {len(html_text)} bytes")
#                         attrs_to_use = primary_attributes or []
#                         if attribute_chunk:
#                             other_attrs = [
#                                 a for a in attrs_to_use if a not in attribute_chunk]
#                             attrs_to_use = attribute_chunk + other_attrs
#                         prompt_config = build_extraction_prompt(
#                             product_name=title,
#                             mpn=mpn,
#                             brand=brand or "",
#                             taxonomy=taxonomy or "",
#                             primary_attributes=attrs_to_use,
#                             html_content=html_text,
#                             candidate_images=candidate_images
#                         )
#                         extraction_result = await call_llm_with_schema(
#                             prompt=prompt_config['prompt'],
#                             response_model="ExtractionResponse",
#                             llm_provider=llm_provider,
#                             estimated_tokens=3000
#                         )
#                         attr_dicts = []
#                         image_url = None
#                         if extraction_result and extraction_result.product_detected:
#                             if hasattr(extraction_result, 'image_url'):
#                                 image_url = extraction_result.image_url
#                             for attr in extraction_result.attributes:
#                                 attr_dicts.append({
#                                     'name': attr.name,
#                                     'value': attr.value,
#                                     'unit': attr.unit if hasattr(attr, 'unit') else None,
#                                     'confidence': attr.confidence if hasattr(attr, 'confidence') else 0.9
#                                 })
#                         if not image_url:
#                             image_url = await extract_best_image(html_text, url, mpn)
#                             if image_url:
#                                 logger.info(
#                                     f"Fallback extracted image: {image_url}")
#                         domain = urlparse(url).netloc
#                         return {
#                             'url': url,
#                             'domain': domain,
#                             'attributes': attr_dicts,
#                             'image_url': image_url,
#                             'source_type': 'html'
#                         }
#                     elif content['type'] == 'pdf':
#                         from app.aggregation.services.pdf_service import PDFExtractionService
#                         pdf_service = PDFExtractionService(max_pages=10)
#                         pdf_text = await pdf_service.extract_text(content['raw_bytes'])
#                         if pdf_text and len(pdf_text.strip()) > 100:
#                             logger.info(
#                                 f"Extracted {len(pdf_text)} chars from PDF")
#                             attrs_to_use = primary_attributes or []
#                             if attribute_chunk:
#                                 other_attrs = [
#                                     a for a in attrs_to_use if a not in attribute_chunk]
#                                 attrs_to_use = attribute_chunk + other_attrs
#                             prompt_config = build_pdf_extraction_prompt(
#                                 product_name=title,
#                                 mpn=mpn,
#                                 brand=brand or "",
#                                 taxonomy=taxonomy or "",
#                                 primary_attributes=attrs_to_use,
#                                 pdf_text=pdf_text
#                             )
#                             extraction_result = await call_llm_with_schema(
#                                 prompt=prompt_config['prompt'],
#                                 response_model="ExtractionResponse",
#                                 llm_provider=llm_provider,
#                                 estimated_tokens=4000
#                             )
#                             if extraction_result and extraction_result.product_detected:
#                                 attr_dicts = []
#                                 image_url = None
#                                 if hasattr(extraction_result, 'image_url'):
#                                     image_url = extraction_result.image_url
#                                 for attr in extraction_result.attributes:
#                                     attr_dicts.append({
#                                         'name': attr.name,
#                                         'value': attr.value,
#                                         'unit': getattr(attr, 'unit', None),
#                                         'confidence': getattr(attr, 'confidence', 0.95)
#                                     })
#                                 domain = urlparse(url).netloc
#                                 return {
#                                     'url': url,
#                                     'domain': domain,
#                                     'attributes': attr_dicts,
#                                     'image_url': image_url,
#                                     'source_type': 'pdf'
#                                 }
#                     return None
#                 except Exception as e:
#                     logger.warning(f"Extraction failed for {url}: {e}")
#                     return None
#         tasks = [process_url(url) for url in urls[:5]]
#         results = await asyncio.gather(*tasks)
#         all_extractions = [r for r in results if r is not None]
#         if not all_extractions:
#             return {
#                 'status': 'failed',
#                 'reason': 'No valid extractions',
#                 'golden_record': {'attributes': {}}
#             }
#         logger.info(
#             f"Stage 2 extracted {sum(len(s['attributes']) for s in all_extractions)} total attributes")
#         logger.info("Stage 3: Combined Cleaning, Unification & Standardization")
#         raw_attrs_for_combine = []
#         for src_idx, source in enumerate(all_extractions):
#             for attr in source['attributes']:
#                 raw_attrs_for_combine.append({
#                     'temp_id': f"{src_idx}_{len(raw_attrs_for_combine)}",
#                     'name': attr['name'],
#                     'value': attr['value'],
#                     'unit': attr.get('unit'),
#                     'source_url': source['url'],
#                     'confidence': attr.get('confidence', 0.9)
#                 })
#         project = await db.get(Project, project_id) if db and project_id else None
#         use_case = project.use_case.lower() if project and project.use_case else ""
#         combine_prompt = _build_combined_prompt(
#             raw_attrs_for_combine, brand, mpn, title, taxonomy, existing_excel_attrs=existing_excel_attrs, use_case=use_case)
#         async for attempt in AsyncRetrying(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10)):
#             with attempt:
#                 combined_result = await call_llm_with_schema(
#                     prompt=combine_prompt,
#                     response_model="UnifiedStandardizedResponse",
#                     llm_provider=llm_provider,
#                     estimated_tokens=3000 + len(raw_attrs_for_combine) * 200,
#                     max_tokens=4000
#                 )
#         golden_attributes = combined_result.attributes
#         valid_source_urls = {source['url'] for source in all_extractions}
#         for attr in golden_attributes:
#             if hasattr(attr, 'sources') and attr.sources:
#                 attr.sources = [
#                     src for src in attr.sources if src in valid_source_urls]
#         logger.info(
#             f"Stage 3 produced {len(golden_attributes)} unified attributes")
#         validation_conflicts = {}
#         excel_overrides = {}
#         if "back filling" in use_case or "validation" in use_case:
#             logger.info("Stage 4: Excel Validation")
#             stmt = select(Product).where(Product.product_code == mpn)
#             result = await db.execute(stmt)
#             product = result.scalars().first()
#             excel_attrs = {}
#             if product:
#                 try:
#                     val_stmt=(select(Attribute.attribute_name,AttributeValue.value).join(AttributeValue,AttributeValue.attribute_id==Attribute.id).join(ProductAttributeValueLinkModel,ProductAttributeValueLinkModel.attribute_value_id==AttributeValue.id).where(ProductAttributeValueLinkModel.product_id==product.id))
#                     val_result=await db.execute(val_stmt)
#                     for attr_name,attr_value in val_result.all():
#                         excel_attrs[attr_name]=attr_value
#                 except Exception as e:
#                     logger.warning(f"Failed to read Excel attributes from normalized tables: {e}")

#                 # for attr in product.dynamic_attributes:
#                 #     if isinstance(attr, dict) and attr.get('name'):
#                 #         excel_attrs[attr['name']] = attr.get('value', '')
#             web_attrs = {attr.name: attr.value for attr in golden_attributes}
#             validation_config = build_validation_prompt(
#                 excel_attributes=excel_attrs,
#                 web_attributes=web_attrs,
#                 mpn=mpn,
#                 taxonomy=taxonomy or ""
#             )
#             validation_result = await call_llm_with_schema(
#                 prompt=validation_config['prompt'],
#                 response_model="ValidationResponse",
#                 llm_provider=llm_provider,
#                 estimated_tokens=1500
#             )
#             if "back filling" in use_case and "validation" not in use_case:
#                 for val in validation_result.validations:
#                     if not val.matches and val.recommendation == "use_web":
#                         excel_overrides[val.attribute_name] = val.web_value
#                         validation_conflicts[val.attribute_name] = val.web_value
#                         for attr in golden_attributes:
#                             if attr.name == val.attribute_name:
#                                 attr.value = val.web_value
#             elif "validation" in use_case and "back filling" not in use_case:
#                 for val in validation_result.validations:
#                     if not val.matches and val.recommendation == "use_web":
#                         validation_conflicts[val.attribute_name] = val.web_value
#             elif "back filling" in use_case and "validation" in use_case:
#                 for val in validation_result.validations:
#                     if not val.matches and val.recommendation == "use_web":
#                         excel_overrides[val.attribute_name] = val.web_value
#                         validation_conflicts[val.attribute_name] = val.web_value
#                         for attr in golden_attributes:
#                             if attr.name == val.attribute_name:
#                                 attr.value = val.web_value
#             else:
#                 for val in validation_result.validations:
#                     if not val.matches and val.recommendation == "use_web":
#                         validation_conflicts[val.attribute_name] = val.web_value
#         logger.info("Stage 5: Multi-source Aggregation")
#         if golden_attributes:
#             avg_conf = sum(a.confidence for a in golden_attributes) / \
#                 len(golden_attributes)
#         else:
#             avg_conf = 0.0
#         golden_attr_dicts = [
#             {
#                 'name': a.name,
#                 'value': a.value,
#                 'unit': a.unit,
#                 'confidence': a.confidence,
#                 'sources': a.sources
#             }
#             for a in golden_attributes
#         ]
#         logger.info("Stage 6: Marketing Enrichment")
#         enrichment_config = build_enrichment_prompt(
#             golden_attributes=golden_attr_dicts,
#             product_name=title,
#             brand=brand or "",
#             taxonomy=taxonomy or ""
#         )
#         enrichment_result = await call_llm_with_schema(
#             prompt=enrichment_config['prompt'],
#             response_model="EnrichmentResponse",
#             llm_provider=llm_provider,
#             estimated_tokens=2000,
#             max_tokens=4000
#         )
#         best_image = extract_best_image_fallback(all_extractions)
#         if not best_image and candidate_images:
#             for candidate in candidate_images:
#                 is_valid = await validate_image_url(candidate)
#                 if is_valid:
#                     logger.info(f"Fallback to SearXNG image: {candidate}")
#                     best_image = candidate
#                     break
#         return {
#             'status': 'success',
#             'golden_record': {
#                 'attributes': {attr['name']: attr for attr in golden_attr_dicts},
#                 'short_description': enrichment_result.short_description or "",
#                 'long_description': enrichment_result.long_description,
#                 'features': enrichment_result.features,
#                 'sources_consulted': list({s['url'] for s in all_extractions}),
#                 'confidence': avg_conf
#             },
#             'validation_conflicts': validation_conflicts,
#             'excel_overrides': excel_overrides,
#             'image_url': best_image,
#             'mode': 'backfill' if 'back filling' in use_case else 'standard'
#         }
#     except Exception as e:
#         logger.error(f"Pipeline failed for {mpn}: {e}", exc_info=True)
#         return {
#             'status': 'failed',
#             'reason': str(e),
#             'golden_record': {'attributes': {}}
#         }


# async def aggregate_product(
#     mpn: str,
#     title: str,
#     sku: Optional[str] = None,
#     upc: Optional[str] = None,
#     brand: Optional[str] = None,
#     taxonomy: Optional[str] = None,
#     primary_attributes: Optional[List[str]] = None,
#     db: Optional[AsyncSession] = None,
#     project_id: str = None,
#     llm_provider: str = 'openai',
#     attribute_chunk: Optional[List[str]] = None,
#     existing_excel_attrs: Optional[Dict[str, str]] = None,
#     missing_llm_provider: str = None
# ) -> Dict:
#     try:
#         if missing_llm_provider is None:
#             missing_llm_provider = llm_provider
#         logger.info(f"Starting aggregation for {mpn}")
#         logger.info("Stage 1: URL Discovery")
#         brand_prompt_text = None
#         if brand and db:
#             brand_stmt = select(BrandPrompt.prompt_text).join(
#                 Brand, BrandPrompt.brand_id == Brand.id
#             ).where(
#                 func.lower(Brand.name) == func.lower(brand),
#                 BrandPrompt.status == RuleStatus.ACTIVE
#             )
#             brand_result = await db.execute(brand_stmt)
#             brand_row = brand_result.first()
#             if brand_row:
#                 brand_prompt_text = brand_row[0]

#         # Fetch category prompt
#         category_prompt_text = None
#         if taxonomy and db:
#             taxonomy_parts = [t.strip().lower() for t in taxonomy.split(">")]
#             cat_stmt = select(CategoryPrompt.prompt_text).join(
#                 Category, CategoryPrompt.category_id == Category.id
#             ).where(
#                 func.lower(Category.name).in_(taxonomy_parts),
#                 CategoryPrompt.status == RuleStatus.ACTIVE
#             ).order_by(Category.level.desc())  # Prefer deepest match
#             cat_result = await db.execute(cat_stmt)
#             cat_row = cat_result.first()
#             if cat_row:
#                 category_prompt_text = cat_row[0]
#         search_service = SmartSearchService(llm_provider, db=db, max_results=5)
#         query = title if (
#             mpn in title and brand in title) else f"{brand} {mpn} {title}"
#         query = query.strip()
#         urls, candidate_images = await search_service.get_urls(query, mpn=mpn, brand=brand, title=title, sku=sku, brand_prompt_text=brand_prompt_text, category_prompt_text=category_prompt_text, taxonomy=taxonomy)
#         if not urls:
#             return {
#                 'status': 'failed',
#                 'reason': 'No sources found',
#                 'golden_record': {'attributes': {}}
#             }
#         logger.info(f"Stage 2: Download & Extraction from {len(urls)} sources")
#         download_service = HttpDownloadService(
#             timeout=30,
#         )
#         all_extractions = []
#         _url_semaphore = asyncio.Semaphore(2)

#         async def process_url(url):
#             async with _url_semaphore:
#                 try:
#                     content = await download_service.download(url)
#                     if content is None:
#                         return None
#                     if content['type'] == 'html':
#                         html_text = content['raw_bytes'].decode(
#                             'utf-8', errors='ignore')
#                         logger.info(
#                             f"Downloaded HTML from {url} - size: {len(html_text)} bytes")
#                         attrs_to_use = primary_attributes or []
#                         if attribute_chunk:
#                             other_attrs = [
#                                 a for a in attrs_to_use if a not in attribute_chunk]
#                             attrs_to_use = attribute_chunk + other_attrs
#                         prompt_config = build_extraction_prompt(
#                             product_name=title,
#                             mpn=mpn,
#                             brand=brand or "",
#                             taxonomy=taxonomy or "",
#                             primary_attributes=attrs_to_use,
#                             html_content=html_text,
#                             candidate_images=candidate_images
#                         )
#                         extraction_result = await call_llm_with_schema(
#                             prompt=prompt_config['prompt'],
#                             response_model="ExtractionResponse",
#                             llm_provider=llm_provider,
#                             estimated_tokens=3000
#                         )
#                         attr_dicts = []
#                         image_url = None
#                         if extraction_result and extraction_result.product_detected:
#                             if hasattr(extraction_result, 'image_url'):
#                                 image_url = extraction_result.image_url
#                             for attr in extraction_result.attributes:
#                                 attr_dicts.append({
#                                     'name': attr.name,
#                                     'value': attr.value,
#                                     'unit': attr.unit if hasattr(attr, 'unit') else None,
#                                     'confidence': attr.confidence if hasattr(attr, 'confidence') else 0.9
#                                 })
#                         if not image_url:
#                             image_url = await extract_best_image(html_text, url, mpn)
#                             if image_url:
#                                 logger.info(
#                                     f"Fallback extracted image: {image_url}")
#                         domain = urlparse(url).netloc
#                         return {
#                             'url': url,
#                             'domain': domain,
#                             'attributes': attr_dicts,
#                             'image_url': image_url,
#                             'source_type': 'html'
#                         }
#                     elif content['type'] == 'pdf':
#                         from app.aggregation.services.pdf_service import PDFExtractionService
#                         pdf_service = PDFExtractionService(max_pages=10)
#                         pdf_text = await pdf_service.extract_text(content['raw_bytes'])
#                         if pdf_text and len(pdf_text.strip()) > 100:
#                             logger.info(
#                                 f"Extracted {len(pdf_text)} chars from PDF")
#                             attrs_to_use = primary_attributes or []
#                             if attribute_chunk:
#                                 other_attrs = [
#                                     a for a in attrs_to_use if a not in attribute_chunk]
#                                 attrs_to_use = attribute_chunk + other_attrs
#                             prompt_config = build_pdf_extraction_prompt(
#                                 product_name=title,
#                                 mpn=mpn,
#                                 brand=brand or "",
#                                 taxonomy=taxonomy or "",
#                                 primary_attributes=attrs_to_use,
#                                 pdf_text=pdf_text
#                             )
#                             extraction_result = await call_llm_with_schema(
#                                 prompt=prompt_config['prompt'],
#                                 response_model="ExtractionResponse",
#                                 llm_provider=llm_provider,
#                                 estimated_tokens=4000
#                             )
#                             if extraction_result and extraction_result.product_detected:
#                                 attr_dicts = []
#                                 image_url = None
#                                 if hasattr(extraction_result, 'image_url'):
#                                     image_url = extraction_result.image_url
#                                 for attr in extraction_result.attributes:
#                                     attr_dicts.append({
#                                         'name': attr.name,
#                                         'value': attr.value,
#                                         'unit': getattr(attr, 'unit', None),
#                                         'confidence': getattr(attr, 'confidence', 0.95)
#                                     })
#                                 domain = urlparse(url).netloc
#                                 return {
#                                     'url': url,
#                                     'domain': domain,
#                                     'attributes': attr_dicts,
#                                     'image_url': image_url,
#                                     'source_type': 'pdf'
#                                 }
#                     return None
#                 except Exception as e:
#                     logger.warning(f"Extraction failed for {url}: {e}")
#                     return None
#         tasks = [process_url(url) for url in urls[:5]]
#         results = await asyncio.gather(*tasks)
#         all_extractions = [r for r in results if r is not None]
#         if not all_extractions:
#             return {
#                 'status': 'failed',
#                 'reason': 'No valid extractions',
#                 'golden_record': {'attributes': {}}
#             }
#         logger.info(
#             f"Stage 2 extracted {sum(len(s['attributes']) for s in all_extractions)} total attributes")
#         logger.info("Stage 3: Combined Cleaning, Unification & Standardization")
#         raw_attrs_for_combine = []
#         for src_idx, source in enumerate(all_extractions):
#             for attr in source['attributes']:
#                 raw_attrs_for_combine.append({
#                     'temp_id': f"{src_idx}_{len(raw_attrs_for_combine)}",
#                     'name': attr['name'],
#                     'value': attr['value'],
#                     'unit': attr.get('unit'),
#                     'source_url': source['url'],
#                     'confidence': attr.get('confidence', 0.9)
#                 })
#         project = await db.get(Project, project_id) if db and project_id else None
#         use_case = project.use_case.lower() if project and project.use_case else ""
#         combine_prompt = _build_combined_prompt(
#             raw_attrs_for_combine, brand, mpn, title, taxonomy, existing_excel_attrs=existing_excel_attrs, use_case=use_case)
#         async for attempt in AsyncRetrying(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10)):
#             with attempt:
#                 combined_result = await call_llm_with_schema(
#                     prompt=combine_prompt,
#                     response_model="UnifiedStandardizedResponse",
#                     llm_provider=llm_provider,
#                     estimated_tokens=3000 + len(raw_attrs_for_combine) * 200,
#                     max_tokens=4000
#                 )
#         golden_attributes = combined_result.attributes
#         valid_source_urls = {source['url'] for source in all_extractions}
#         for attr in golden_attributes:
#             if hasattr(attr, 'sources') and attr.sources:
#                 attr.sources = [
#                     src for src in attr.sources if src in valid_source_urls]
#         logger.info(
#             f"Stage 3 produced {len(golden_attributes)} unified attributes")
#         validation_conflicts = {}
#         excel_overrides = {}
#         if "back filling" in use_case or "validation" in use_case:
#             logger.info("Stage 4: Excel Validation")
#             stmt = select(Product).where(Product.product_code == mpn)
#             result = await db.execute(stmt)
#             product = result.scalars().first()
#             excel_attrs = {}
#             if product:
#                 try:
#                     val_stmt = (select(Attribute.attribute_name, AttributeValue.value).join(AttributeValue, AttributeValue.attribute_id == Attribute.id).join(
#                         ProductAttributeValueLinkModel, ProductAttributeValueLinkModel.attribute_value_id == AttributeValue.id).where(ProductAttributeValueLinkModel.product_id == product.id))
#                     val_result = await db.execute(val_stmt)
#                     for attr_name, attr_value in val_result.all():
#                         excel_attrs[attr_name] = attr_value
#                 except Exception as e:
#                     logger.warning(
#                         f"Failed to read Excel attributes from normalized tables: {e}")

#                 # for attr in product.dynamic_attributes:
#                 #     if isinstance(attr, dict) and attr.get('name'):
#                 #         excel_attrs[attr['name']] = attr.get('value', '')
#             web_attrs = {attr.name: attr.value for attr in golden_attributes}
#             validation_config = build_validation_prompt(
#                 excel_attributes=excel_attrs,
#                 web_attributes=web_attrs,
#                 mpn=mpn,
#                 taxonomy=taxonomy or ""
#             )
#             validation_result = await call_llm_with_schema(
#                 prompt=validation_config['prompt'],
#                 response_model="ValidationResponse",
#                 llm_provider=missing_llm_provider, 
#                 estimated_tokens=1500
#             )
#             if "back filling" in use_case and "validation" not in use_case:
#                 for val in validation_result.validations:
#                     if not val.matches and val.recommendation == "use_web":
#                         excel_overrides[val.attribute_name] = val.web_value
#                         validation_conflicts[val.attribute_name] = val.web_value
#                         for attr in golden_attributes:
#                             if attr.name == val.attribute_name:
#                                 attr.value = val.web_value
#             elif "validation" in use_case and "back filling" not in use_case:
#                 for val in validation_result.validations:
#                     if not val.matches and val.recommendation == "use_web":
#                         validation_conflicts[val.attribute_name] = val.web_value
#             elif "back filling" in use_case and "validation" in use_case:
#                 for val in validation_result.validations:
#                     if not val.matches and val.recommendation == "use_web":
#                         excel_overrides[val.attribute_name] = val.web_value
#                         validation_conflicts[val.attribute_name] = val.web_value
#                         for attr in golden_attributes:
#                             if attr.name == val.attribute_name:
#                                 attr.value = val.web_value
#             else:
#                 for val in validation_result.validations:
#                     if not val.matches and val.recommendation == "use_web":
#                         validation_conflicts[val.attribute_name] = val.web_value
#         logger.info("Stage 5: Multi-source Aggregation")
#         if golden_attributes:
#             avg_conf = sum(a.confidence for a in golden_attributes) / \
#                 len(golden_attributes)
#         else:
#             avg_conf = 0.0
#         golden_attr_dicts = [
#             {
#                 'name': a.name,
#                 'value': a.value,
#                 'unit': a.unit,
#                 'confidence': a.confidence,
#                 'sources': a.sources
#             }
#             for a in golden_attributes
#         ]
#         logger.info("Stage 6: Marketing Enrichment")
#         enrichment_config = build_enrichment_prompt(
#             golden_attributes=golden_attr_dicts,
#             product_name=title,
#             brand=brand or "",
#             taxonomy=taxonomy or ""
#         )
#         enrichment_result = await call_llm_with_schema(
#             prompt=enrichment_config['prompt'],
#             response_model="EnrichmentResponse",
#             llm_provider=missing_llm_provider,  
#             estimated_tokens=2000,
#             max_tokens=4000
#         )
#         best_image = extract_best_image_fallback(all_extractions)
#         if not best_image and candidate_images:
#             for candidate in candidate_images:
#                 is_valid = await validate_image_url(candidate)
#                 if is_valid:
#                     logger.info(f"Fallback to SearXNG image: {candidate}")
#                     best_image = candidate
#                     break
#         return {
#             'status': 'success',
#             'golden_record': {
#                 'attributes': {attr['name']: attr for attr in golden_attr_dicts},
#                 'short_description': enrichment_result.short_description or "",
#                 'long_description': enrichment_result.long_description,
#                 'features': enrichment_result.features,
#                 'sources_consulted': list({s['url'] for s in all_extractions}),
#                 'confidence': avg_conf
#             },
#             'validation_conflicts': validation_conflicts,
#             'excel_overrides': excel_overrides,
#             'image_url': best_image,
#             'mode': 'backfill' if 'back filling' in use_case else 'standard'
#         }
#     except Exception as e:
#         logger.error(f"Pipeline failed for {mpn}: {e}", exc_info=True)
#         return {
#             'status': 'failed',
#             'reason': str(e),
#             'golden_record': {'attributes': {}}
#         }
def extract_domains_and_generate_urls(prompt_text: str) -> List[str]:
    """Extract site: domains from prompt and generate search URLs."""
    if not prompt_text:
        return []
    
    # Extract domains from site: operators
    import re
    domains = re.findall(r'site:([a-zA-Z0-9][-a-zA-Z0-9]*\.[a-zA-Z]{2,})', prompt_text)
    
    # Generate search URLs for each domain
    urls = []
    for domain in domains:
        # Generic search URL pattern for the domain
        urls.append(f"https://{domain}/")
    
    return urls
import re
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
#     llm_provider: str
# ) -> Optional[str]:
#     """
#     Use LLM to navigate a domain and find the product page.
#     """
#     from app.llm import call_llm_with_schema
#     from pydantic import BaseModel
#     from app.aggregation.services.download_service import HttpDownloadService
    

    
    
#     try:
#         download_service = HttpDownloadService(timeout=30)
        
#         # Step 1: Download homepage
#         content = await download_service.download(domain_url)
#         if not content or content['type'] != 'html':
#             return None
        
#         html_text = content['raw_bytes'].decode('utf-8', errors='ignore')[:100000]
        
#         # Step 2: Let LLM analyze and find navigation path
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
        
#         if result and result.product_url and result.confidence > 0.5:
#             logger.info(f"LLM found URL on {domain_url}: {result.product_url}")
#             return result.product_url
            
#     except Exception as e:
#         logger.warning(f"LLM navigation failed for {domain_url}: {e}")
    
#     return None
async def find_product_page_with_llm(
    domain_url: str,
    mpn: str,
    brand: str,
    title: str,
    llm_provider: str,
    upc: Optional[str] = None   # <-- new optional parameter
) -> Optional[str]:
    """
    Use LLM to navigate a domain and find the product page.
    Returns a single product URL (if found with confidence > 0.5)
    and logs a content‑based score (0‑100) based on brand/MPN/UPC.
    """
    from app.llm import call_llm_with_schema
    from pydantic import BaseModel
    from app.aggregation.services.download_service import HttpDownloadService
    
    

    
    
    try:
        download_service = HttpDownloadService(timeout=30)
        
        # Step 1: Download homepage
        content = await download_service.download(domain_url)
        if not content or content['type'] != 'html':
            return None
        
        html_text = content['raw_bytes'].decode('utf-8', errors='ignore')[:100000]
        
        # Step 2: LLM finds the most promising product page URL
        prompt = f"""
You are a web navigation expert. Find the product page for this product on {domain_url}.

Product:
- MPN: {mpn}
- Brand: {brand}
- Name: {title}

Homepage HTML (first 100k chars):
{html_text}

Task:
1. Look for search forms, navigation menus, category links
2. Find the most likely path to reach the product page
3. If there's a search function, determine the search URL pattern
4. Return the most promising product page URL or search URL

Rules:
- If you find an exact URL containing the MPN, return it immediately
- If you find a category page (e.g., /hex-nuts), return that
- If you find a search form, construct the search URL with MPN
- Be specific - return full URLs

Return JSON with:
- product_url: the URL to try (full URL)
- confidence: 0.0-1.0
- reasoning: why you chose this URL
"""
        
        result = await call_llm_with_schema(
            prompt=prompt,
            response_model="ProductPageResponse",
            llm_provider=llm_provider,
            estimated_tokens=4000
        )
        
        if not result or not result.product_url or result.confidence <= 0.5:
            return None
        
        candidate_url = result.product_url
        logger.info(f"LLM found URL on {domain_url}: {candidate_url} (nav confidence={result.confidence:.2f})")
        
        # ----- NEW: Score the candidate page (brand + MPN + optional UPC) -----
        page_content = await download_service.download(candidate_url)
        if page_content and page_content['type'] == 'html':
            page_html = page_content['raw_bytes'].decode('utf-8', errors='ignore')[:200000]
            
            upc_text = f"- UPC: {upc}" if upc else "- UPC: Not provided"
            score_prompt = f"""
You are evaluating a product page to see if it matches the given product.

Product:
- Brand: {brand}
- MPN: {mpn}
{upc_text}

Page URL: {candidate_url}
Page HTML (first 200k chars):
{page_html}

Task:
1. Determine if the brand name appears clearly on the page (text, meta, structured data).
2. Determine if the MPN or Brand appears clearly on the page OR in the URL.
3. Determine if the UPC appears (if provided).

Then assign a score **exactly** following these rules:
- If brand is found AND MPN is found → score = 90
- If UPC is also found → add 10 → total 100
- If only brand found OR only MPN found → score = 50
- If neither found → score = 0

Return JSON:
{{
    "brand_found": bool,
    "mpn_found": bool,
    "upc_found": bool,
    "score": int,
    "reasoning": "short explanation"
}}
"""
            score_result = await call_llm_with_schema(
                prompt=score_prompt,
                response_model="PageMatchScore",
                llm_provider=llm_provider,
                estimated_tokens=800
            )
            if score_result:
                logger.info(f"Content score for {candidate_url}: {score_result.score} – {score_result.reasoning}")
            else:
                logger.warning(f"Scoring failed for {candidate_url}")
        else:
            logger.warning(f"Could not fetch candidate page for scoring: {candidate_url}")
        
        # Return the URL regardless of score (no filtering)
        return candidate_url
            
    except Exception as e:
        logger.warning(f"LLM navigation failed for {domain_url}: {e}")
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

        # Fetch category prompt
        # category_prompt_text = None
        # selected_taxonomy = None  # ← ADD THIS
        # if taxonomy and db:
        #     taxonomy_parts = [t.strip().lower() for t in taxonomy.split(">")]
        #     logger.info(f"Looking for category prompt with taxonomy parts: {taxonomy_parts}") 
        #     cat_stmt = select(
        #         CategoryPrompt.prompt_text, 
        #         CategoryPrompt.selected_taxonomy  # ← ADD THIS
        #     ).join(
        #         Category, CategoryPrompt.category_id == Category.id
        #     ).where(
        #         func.lower(Category.name).in_(taxonomy_parts),
        #         CategoryPrompt.status == RuleStatus.ACTIVE
        #     ).order_by(Category.level.desc())
        #     cat_result = await db.execute(cat_stmt)
        #     cat_row = cat_result.first()
        #     if cat_row:
        #         category_prompt_text = cat_row[0]
        #         selected_taxonomy = cat_row[1]
        #         logger.info(f"Found category prompt! selected_taxonomy: {selected_taxonomy}")
        #     else:
        #         logger.info(f"No category prompt found for taxonomy parts: {taxonomy_parts}") 
        # else:
        #     logger.info(f"Taxonomy is None or db is None: taxonomy={taxonomy}, db={db is not None}") 
        # Fetch category prompt by matching selected_taxonomy as prefix of product taxonomy
# ── Fetch category prompt by matching taxonomy OR category ──
        category_prompt_text = None
        selected_taxonomy = None
        matched_category_id = None

        if taxonomy and db:
            clean_taxonomy = taxonomy.strip()
            
            # ── 1. Try taxonomy-mode (longest prefix wins) ──
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
                logger.info(f"✓ Taxonomy prompt matched: '{selected_taxonomy}'")
            
            # ── 2. Fallback to category-mode ──
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
                        logger.info(f"✓ Category prompt matched: '{category.name}'")
                        break
            
            if not category_prompt_text:
                logger.info(f"✗ No prompt found for taxonomy: '{clean_taxonomy}'")
        search_service = SmartSearchService(llm_provider, db=db, max_results=5)
        query = title if (
            mpn in title and brand in title) else f"{brand} {mpn} {title}"
        query = query.strip()
        direct_urls = []
        if brand_prompt_text:
            direct_urls = extract_urls_from_prompt(brand_prompt_text)
            if not direct_urls:
                # If no https:// URLs, try to generate from site: domains
                direct_urls = extract_domains_and_generate_urls(brand_prompt_text)
        elif category_prompt_text:
            direct_urls = extract_urls_from_prompt(category_prompt_text)
            if not direct_urls:
                direct_urls = extract_domains_and_generate_urls(category_prompt_text)

        if direct_urls:
            logger.info(f"Generated {len(direct_urls)} URLs from domains for {mpn}: {direct_urls}") 
            product_urls = []
            for domain_url in direct_urls:
                product_url = await find_product_page_with_llm(domain_url, mpn, brand, title, llm_provider)
                if product_url:
                    product_urls.append(product_url)
            
            if product_urls:
                direct_urls = product_urls
                logger.info(f"LLM found {len(direct_urls)} product URLs: {direct_urls}")
        urls, candidate_images = await search_service.get_urls(
    query, mpn=mpn, brand=brand, sku=sku, 
    brand_prompt_text=brand_prompt_text, 
    category_prompt_text=category_prompt_text, 
    taxonomy=taxonomy,direct_urls=direct_urls,selected_taxonomy=selected_taxonomy
)
        if not urls:
            return {
                'status': 'failed',
                'reason': 'No sources found',
                'golden_record': {'attributes': {}}
            }
        logger.info(f"Stage 2: Download & Extraction from {len(urls)} sources")
        download_service = HttpDownloadService(
            timeout=30,
        )
        all_extractions = []
        _url_semaphore = asyncio.Semaphore(2)

        async def process_url(url):
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
                            candidate_images=candidate_images
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
                            logger.info(f"Image URL: {extraction_result.image_url if hasattr(extraction_result, 'image_url') else 'None'}")
                            logger.info(f"Number of attributes extracted: {len(extraction_result.attributes)}")
                            for attr in extraction_result.attributes:
                                logger.info(f"  - {attr.name}: {attr.value} {getattr(attr, 'unit', '')} (confidence: {getattr(attr, 'confidence', 0.9)})")
                        else:
                            logger.info(f"Product detected: NO")
                            logger.info(f"Extraction result: {extraction_result}")
                        logger.info(f"=====================================")
                        attr_dicts = []
                        image_url = None
                        if extraction_result and extraction_result.product_detected:
                            if hasattr(extraction_result, 'image_url'):
                                image_url = extraction_result.image_url
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
                            'source_type': 'html'
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

                # for attr in product.dynamic_attributes:
                #     if isinstance(attr, dict) and attr.get('name'):
                #         excel_attrs[attr['name']] = attr.get('value', '')
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
        logger.info("Stage 6: Marketing Enrichment")
        enrichment_config = build_enrichment_prompt(
            golden_attributes=golden_attr_dicts,
            product_name=title,
            brand=brand or "",
            taxonomy=taxonomy or ""
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

# async def aggregate_product(
#     mpn: str,
#     title: str,
#     sku: Optional[str] = None,
#     upc: Optional[str] = None,
#     brand: Optional[str] = None,
#     taxonomy: Optional[str] = None,
#     primary_attributes: Optional[List[str]] = None,
#     db: Optional[AsyncSession] = None,
#     project_id: str = None,
#     llm_provider: str = 'openai',
#     attribute_chunk: Optional[List[str]] = None,
#     existing_excel_attrs: Optional[Dict[str, str]] = None
# ) -> Dict:
#     try:
#         if not db:
#             raise ValueError("DB session required for dynamic prompt system.")
#         project = await db.get(Project, project_id) if db and project_id else None
#         operation_mode = (project.operation_mode or "aggregation").lower() if project else "aggregation"
#         use_case = project.use_case if project and project.use_case else ""

#         engine = RuleEngine(db)
#         if operation_mode == 'enrichment':
#             validation_conflicts = {}
#             excel_overrides = {}
#             golden_attributes = []  # Define this

#             if "back filling" in use_case or "validation" in use_case:
#                 logger.info("Stage: Excel Validation")
#                 stmt = select(Product).where(Product.product_code == mpn)
#                 result = await db.execute(stmt)
#                 product = result.scalars().first()
#                 excel_attrs = {}
#                 if product and product.dynamic_attributes:
#                     for attr in product.dynamic_attributes:
#                         if isinstance(attr, dict) and attr.get('name'):
#                             excel_attrs[attr['name']] = attr.get('value', '')

#                 # Use Excel attributes as golden attributes
#                 golden_attributes = []
#                 for name, value in excel_attrs.items():
#                     golden_attributes.append({
#                         'name': name,
#                         'value': value,
#                         'unit': '',
#                         'confidence': 1.0,
#                         'sources': ['excel']
#                     })

#                 # Web attributes - empty for enrichment mode
#                 web_attrs = {}
#                 comparisons = []
#                 for attr_name in excel_attrs.keys():
#                     excel_val = excel_attrs.get(attr_name, "")
#                     web_val = web_attrs.get(attr_name, "NOT FOUND")
#                     comparisons.append(f"  • {attr_name}:")
#                     comparisons.append(f"      Excel: {excel_val}")
#                     comparisons.append(f"      Web:   {web_val}")
#                     comparison_text = '\n'.join(comparisons)
#                 validation_prompt = await engine.get_active_prompt(
#                     stage="validation",
#                     operation_mode=operation_mode,
#                     use_case=use_case,
#                     context={
#                         "excel_attributes": excel_attrs,
#                         "web_attributes": web_attrs,
#                         "comparison_text": comparison_text,
#                         "mpn": mpn,
#                         "taxonomy": taxonomy or "",
#                     }
#                 )

#                 if not validation_prompt:
#                     logger.error("No validation prompt configured.")
#                     return {"status": "failed", "reason": "No validation prompt configured"}

#                 validation_result = await call_llm_with_schema(
#                     prompt=validation_prompt,
#                     response_model="ValidationResponse",
#                     llm_provider=llm_provider,
#                     estimated_tokens=1500
#                 )

#                 # Process validation results...
#                 if "back filling" in use_case:
#                     for val in validation_result.validations:
#                         if not val.matches and val.recommendation == "use_web":
#                             excel_overrides[val.attribute_name] = val.web_value
#                             if "validation" in use_case:
#                                 validation_conflicts[val.attribute_name] = val.web_value

#             # Marketing Enrichment
#             attrs_text = "\n".join([f"  • {attr['name']}: {attr['value']}" for attr in golden_attributes])
#             enrichment_prompt = await engine.get_active_prompt(
#                 stage="enrichment",
#                 operation_mode=operation_mode,
#                 use_case=use_case,
#                 context={
#                     "golden_attributes": golden_attributes,
#                     "attrs_text": attrs_text,
#                     "product_name": title,
#                     "brand": brand or "",
#                     "taxonomy": taxonomy or "",
#                 }
#             )

#             if not enrichment_prompt:
#                 logger.error("No enrichment prompt configured.")
#                 return {"status": "failed", "reason": "No enrichment rule configured"}

#             enrichment_result = await call_llm_with_schema(
#                 prompt=enrichment_prompt,
#                 response_model="EnrichmentResponse",
#                 llm_provider=llm_provider,
#                 estimated_tokens=2000,
#                 max_tokens=4000
#             )

#             # ========== RETURN EARLY ==========
#             return {
#                 'status': 'success',
#                 'golden_record': {
#                     'attributes': {attr['name']: attr for attr in golden_attributes},
#                     'short_description': enrichment_result.short_description or "",
#                     'long_description': enrichment_result.long_description,
#                     'features': enrichment_result.features,
#                     'sources_consulted': ['excel_upload'],
#                     'confidence': 1.0
#                 },
#                 'validation_conflicts': validation_conflicts,
#                 'excel_overrides': excel_overrides,
#                 'image_url': None,
#                 'mode': 'enrichment'
#             }
#         logger.info(f"Starting aggregation for {mpn}")
#         logger.info("Stage 1: URL Discovery")
#         search_service = SmartSearchService(llm_provider,db=db, max_results=5)

#         b = (brand or "").strip()
#         if mpn in title and b and b in title:
#             query = title
#         else:
#             query = f"{b} {mpn} {title}".strip()
#         query = query.strip()
#         urls, candidate_images = await search_service.get_urls(
#             query, mpn=mpn, brand=brand, title=title, sku=sku,operation_mode=operation_mode,use_case=use_case
#         )
#         if not urls:
#             return {
#                 'status': 'failed',
#                 'reason': 'No sources found',
#                 'golden_record': {'attributes': {}}
#             }
#         logger.info(f"Stage 2: Download & Extraction from {len(urls)} sources")
#         download_service = HttpDownloadService(
#             timeout=30,
#         )
#         all_extractions = []
#         _url_semaphore = asyncio.Semaphore(2)

#         async def process_url(url):
#             async with _url_semaphore:
#                 try:
#                     content = await download_service.download(url)
#                     if content is None:
#                         return None
#                     if content['type'] == 'html':
#                         html_text = content['raw_bytes'].decode(
#                             'utf-8', errors='ignore')
#                         logger.info(
#                             f"Downloaded HTML from {url} - size: {len(html_text)} bytes")
#                         attrs_to_use = primary_attributes or []
#                         if attribute_chunk:
#                             other_attrs = [
#                                 a for a in attrs_to_use if a not in attribute_chunk]
#                             attrs_to_use = attribute_chunk + other_attrs
#                         primary_attrs_display = "\n".join([f"- {attr}" for attr in attrs_to_use]) if attrs_to_use else "None"
#                         candidate_section = ""
#                         if candidate_images:
#                             candidate_section = "\n".join([f"- {img}" for img in candidate_images[:5]])
#                         prompt = await engine.get_active_prompt(
#                         stage="extraction",
#                         operation_mode=operation_mode,
#                         use_case=use_case,
#                         context={
#                             "mpn": mpn,
#                             "brand": brand or "",
#                             "product_name": title,
#                             "taxonomy": taxonomy or "",
#                             "primary_attributes": attrs_to_use,
#                              "primary_attrs_display": primary_attrs_display,
#                             "html_content": html_text,
#                             "candidate_images": candidate_images,
#                             "candidate_section": candidate_section,
#                             "source_url": url,
#                         }
#                         )
#                         if not prompt:
#                             logger.error("No extraction prompt configured.")
#                             return None
#                         extraction_result = await call_llm_with_schema(
#                             prompt=prompt,
#                             response_model="ExtractionResponse",
#                             llm_provider=llm_provider,
#                             estimated_tokens=3000
#                         )
#                         attr_dicts = []
#                         image_url = None
#                         if extraction_result and extraction_result.product_detected:
#                             if hasattr(extraction_result, 'image_url'):
#                                 image_url = extraction_result.image_url
#                             for attr in extraction_result.attributes:
#                                 attr_dicts.append({
#                                     'name': attr.name,
#                                     'value': attr.value,
#                                     'unit': attr.unit if hasattr(attr, 'unit') else None,
#                                     'confidence': attr.confidence if hasattr(attr, 'confidence') else 0.9
#                                 })
#                         if not image_url:
#                             image_url = await extract_best_image(html_text, url, mpn)
#                             if image_url:
#                                 logger.info(
#                                     f"Fallback extracted image: {image_url}")
#                         domain = urlparse(url).netloc
#                         return {
#                             'url': url,
#                             'domain': domain,
#                             'attributes': attr_dicts,
#                             'image_url': image_url,
#                             'source_type': 'html'
#                         }
#                     elif content['type'] == 'pdf':
#                         from app.aggregation.services.pdf_service import PDFExtractionService
#                         pdf_service = PDFExtractionService(max_pages=10)
#                         pdf_text = await pdf_service.extract_text(content['raw_bytes'])
#                         if pdf_text and len(pdf_text.strip()) > 100:
#                             logger.info(
#                                 f"Extracted {len(pdf_text)} chars from PDF")
#                             attrs_to_use = primary_attributes or []
#                             if attribute_chunk:
#                                 other_attrs = [
#                                     a for a in attrs_to_use if a not in attribute_chunk]
#                                 attrs_to_use = attribute_chunk + other_attrs
#                             primary_attrs_display = "\n".join([f"- {attr}" for attr in attrs_to_use]) if attrs_to_use else "None"
#                             candidate_section = ""
#                             if candidate_images:
#                                 candidate_section = "\n".join([f"- {img}" for img in candidate_images[:5]])
#                             prompt = await engine.get_active_prompt(
#                             stage="extraction",
#                             operation_mode=operation_mode,
#                             use_case=use_case,
#                             context={
#                                 "mpn": mpn,
#                                 "brand": brand or "",
#                                 "product_name": title,
#                                 "taxonomy": taxonomy or "",
#                                 "primary_attributes": attrs_to_use,
#                                 "primary_attrs_display": primary_attrs_display,
#                                 "pdf_text": pdf_text,
#                                 "html_content": pdf_text,
#                                 "source_url": url,
#                                 "candidate_images": candidate_images,
#                                 "candidate_section": candidate_section
#                             }
#                         )
#                             if not prompt:
#                                 logger.error("No extraction prompt configured.")
#                                 return {
#                                     "status": "failed",
#                                     "reason": "No extraction rule configured"
#                                 }
#                             extraction_result = await call_llm_with_schema(
#                                 prompt=prompt,
#                                 response_model="ExtractionResponse",
#                                 llm_provider=llm_provider,
#                                 estimated_tokens=4000
#                             )
#                             if extraction_result and extraction_result.product_detected:
#                                 attr_dicts = []
#                                 image_url = None
#                                 if hasattr(extraction_result, 'image_url'):
#                                     image_url = extraction_result.image_url
#                                 for attr in extraction_result.attributes:
#                                     attr_dicts.append({
#                                         'name': attr.name,
#                                         'value': attr.value,
#                                         'unit': getattr(attr, 'unit', None),
#                                         'confidence': getattr(attr, 'confidence', 0.95)
#                                     })
#                                 domain = urlparse(url).netloc
#                                 return {
#                                     'url': url,
#                                     'domain': domain,
#                                     'attributes': attr_dicts,
#                                     'image_url': image_url,
#                                     'source_type': 'pdf'
#                                 }
#                     return None
#                 except Exception as e:
#                     logger.warning(f"Extraction failed for {url}: {e}")
#                     return None
#         tasks = [process_url(url) for url in urls[:5]]
#         results = await asyncio.gather(*tasks)
#         all_extractions = [r for r in results if r is not None]
#         if not all_extractions:
#             return {
#                 'status': 'failed',
#                 'reason': 'No valid extractions',
#                 'golden_record': {'attributes': {}}
#             }
#         logger.info(
#             f"Stage 2 extracted {sum(len(s['attributes']) for s in all_extractions)} total attributes")
#         logger.info("Stage 3: Combined Cleaning, Unification & Standardization")
#         raw_attrs_for_combine = []
#         for src_idx, source in enumerate(all_extractions):
#             for attr in source['attributes']:
#                 raw_attrs_for_combine.append({
#                     'temp_id': f"{src_idx}_{len(raw_attrs_for_combine)}",
#                     'name': attr['name'],
#                     'value': attr['value'],
#                     'unit': attr.get('unit'),
#                     'source_url': source['url'],
#                     'confidence': attr.get('confidence', 0.9)
#                 })
#         attr_lines = []
#         for a in raw_attrs_for_combine:
#             line = f"ID: {a['temp_id']}\n  Name: {a['name']}\n  Value: {a['value']}"
#             if a.get('unit'):
#                 line += f"\n  Unit: {a['unit']}"
#             if a.get('source_url'):
#                 line += f"\n  Source: {a['source_url']}"
#             attr_lines.append(line)
#         attributes_text = "\n\n".join(attr_lines)
#         combine_prompt = await engine.get_active_prompt(
#             stage="combine",
#             operation_mode=operation_mode,
#             use_case=use_case,
#             context={
#                 "raw_attributes": raw_attrs_for_combine,
#                 "attributes_text": attributes_text,
#                 "brand": brand or "",
#                 "mpn": mpn,
#                 "title": title,
#                 "taxonomy": taxonomy or "",
#                 "existing_excel_attrs": existing_excel_attrs or {},
#                 "excel_section": "",
#                  "validation_section": ""
#             }
#         )
#         if not combine_prompt:
#             logger.error("No combine prompt configured.")
#             return {
#                 "status": "failed",
#                 "reason": "No combine rule configured"
#             }
#         async for attempt in AsyncRetrying(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10)):
#             with attempt:
#                 combined_result = await call_llm_with_schema(
#                     prompt=combine_prompt,
#                     response_model="UnifiedStandardizedResponse",
#                     llm_provider=llm_provider,
#                     estimated_tokens = min(8000,3000 + len(raw_attrs_for_combine) * 200),
#                     max_tokens=4000
#                 )
#         golden_attributes = combined_result.attributes
#         valid_source_urls = {source['url'] for source in all_extractions}
#         for attr in golden_attributes:
#             if hasattr(attr, 'sources') and attr.sources:
#                 attr.sources = [
#                     src for src in attr.sources if src in valid_source_urls]
#         logger.info(
#             f"Stage 3 produced {len(golden_attributes)} unified attributes")
#         validation_conflicts = {}
#         excel_overrides = {}

#         logger.info("Stage 5: Multi-source Aggregation")
#         if golden_attributes:
#             avg_conf = sum(a.confidence for a in golden_attributes) / \
#                 len(golden_attributes)
#         else:
#             avg_conf = 0.0
#         simplified_attrs = {}
#         for a in golden_attributes:
#             simplified_attrs[a.name] = {
#                 'value': a.value,
#                 'unit': a.unit if a.unit else '',
#                 'confidence': a.confidence
#             }
#         golden_attr_dicts = [
#             {
#                 'name': a.name,
#                 'value': a.value,
#                 'unit': a.unit,
#                 'confidence': a.confidence,
#                 'sources': a.sources
#             }
#             for a in golden_attributes
#         ]
#         logger.info("Stage 6: Marketing Enrichment")
#         attrs_lines = []
#         for attr in golden_attr_dicts:
#             line = f"{attr['name']}: {attr['value']}"
#             if attr.get('unit'):
#                 line += f" {attr['unit']}"
#             attrs_lines.append(line)
#         attrs_text = "\n".join(attrs_lines)
#         enrichment_prompt = await engine.get_active_prompt(
#             stage="enrichment",
#             operation_mode=operation_mode,
#             use_case=use_case,
#             context={
#                 "golden_attributes": golden_attr_dicts,
#                 "attrs_text": attrs_text,
#                 "product_name": title,
#                 "brand": brand or "",
#                 "taxonomy": taxonomy or "",
#             }
#         )
#         if not enrichment_prompt:
#             logger.error("No enrichment prompt configured.")
#             return {
#                 "status": "failed",
#                 "reason": "No enrichment rule configured"
#             }

#         enrichment_result = await call_llm_with_schema(
#             prompt=enrichment_prompt,
#             response_model="EnrichmentResponse",
#             llm_provider=llm_provider,
#             estimated_tokens=2000,
#             max_tokens=4000
#         )
#         best_image = extract_best_image_fallback(all_extractions)
#         if not best_image and candidate_images:
#             for candidate in candidate_images:
#                 is_valid = await validate_image_url(candidate)
#                 if is_valid:
#                     logger.info(f"Fallback to SearXNG image: {candidate}")
#                     best_image = candidate
#                     break
#         return {
#             'status': 'success',
#             'golden_record': {
#                 'attributes': simplified_attrs,
#                 'short_description': enrichment_result.short_description or "",
#                 'long_description': enrichment_result.long_description,
#                 'features': enrichment_result.features,
#                 'sources_consulted': list({s['url'] for s in all_extractions}),
#                 'confidence': avg_conf
#             },
#             'validation_conflicts': validation_conflicts,
#             'excel_overrides': excel_overrides,
#             'image_url': best_image,
#             'mode': 'backfill' if 'back filling' in use_case else 'standard'
#         }
#     except Exception as e:
#         logger.error(f"Pipeline failed for {mpn}: {e}", exc_info=True)
#         return {
#             'status': 'failed',
#             'reason': str(e),
#             'golden_record': {'attributes': {}}
#         }


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
