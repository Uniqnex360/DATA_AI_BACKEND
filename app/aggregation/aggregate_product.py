from typing import Dict, Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from app.aggregation.pipeline import AggregationPipeline
from sqlmodel import select
from app.aggregation.services.search_service import SerpApiSearchService
from app.aggregation.prompts.cleaning_prompts import build_cleaning_prompt
from app.aggregation.services.download_service import HttpDownloadService
from app.aggregation.services.extraction_service import (
    ExtractionService, HtmlExtractor, PdfExtractor, PlaywrightExtractor, StructuredDataExtractor)
from app.models.product import Product
from app.models.project import Project
import asyncio
from app.aggregation.services.image_service import ImageService
from app.aggregation.prompt_builder import build_aggregation_prompt
import logging
logger = logging.getLogger("aggregate_product")
from app.aggregation.stages import (
    search,
    extraction,
    cleaning,
    unification,
    validation,
    aggregation,
    enrichment
)
_llm_semaphore = asyncio.Semaphore(1)
_url_semaphore = asyncio.Semaphore(2)
from app.aggregation.services.search_service import SerpApiSearchService
from app.aggregation.services.download_service import HttpDownloadService
from app.llm import call_llm_with_schema
def build_pipeline() -> AggregationPipeline:
    return AggregationPipeline(
        search_service=SerpApiSearchService(max_results=5),
        download_service=HttpDownloadService(timeout=30),
        extraction_service=ExtractionService(extractors=[
            HtmlExtractor(),
            PlaywrightExtractor(),
            PdfExtractor(),
        ]),
        image_service=ImageService(),
    )
def chunk_attributes(attributes: List[str], chunk_size: int = 10) -> List[List[str]]:
    return [attributes[i:i + chunk_size] for i in range(0, len(attributes), chunk_size)]
from typing import Dict, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
import logging
from app.aggregation.stages import (
    search,
    extraction,
    cleaning,
    unification,
    validation,
    aggregation,
    enrichment
)
from app.aggregation.services.search_service import SerpApiSearchService
from app.aggregation.services.download_service import HttpDownloadService
from app.llm import call_llm_with_schema
logger = logging.getLogger("aggregate_product")
BLOCKED_KEYWORDS = ['logo', 'icon', 'banner', 'button', 'spacer', 'placeholder', 'loading', 'pixel', 'tracking']
def is_likely_product_image(img_url: str, alt: str = '') -> bool:
    url_lower = img_url.lower()
    alt_lower = alt.lower()
    if any(k in url_lower for k in BLOCKED_KEYWORDS):
        return False
    if alt and any(k in alt_lower for k in BLOCKED_KEYWORDS):
        return False
    product_keywords = ['product', 'main', 'hero', 'full', 'large', 'zoom']
    score = 0
    if any(k in url_lower for k in product_keywords):
        score += 2
    if alt and any(k in alt_lower for k in product_keywords):
        score += 1
    return score >= 2 or (score == 0 and not any(k in url_lower for k in BLOCKED_KEYWORDS))
async def extract_fallback_image(html: str, base_url: str) -> Optional[str]:
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin
    soup = BeautifulSoup(html, 'html.parser')
    candidates = []  
    meta = soup.find('meta', property='og:image')
    if meta and meta.get('content'):
        url = urljoin(base_url, meta['content'])
        candidates.append((url, 100))  
    meta = soup.find('meta', attrs={'name': 'twitter:image'})
    if meta and meta.get('content'):
        url = urljoin(base_url, meta['content'])
        candidates.append((url, 90))
    for img in soup.find_all('img'):
        src = img.get('src')
        if not src:
            continue
        url = urljoin(base_url, src)
        alt = img.get('alt', '')
        score = 0
        if any(k in url.lower() for k in ['product', 'main', 'hero']):
            score += 20
        if alt and any(k in alt.lower() for k in ['product', 'main', 'hero']):
            score += 10
        if any(k in url.lower() for k in BLOCKED_KEYWORDS):
            score -= 50
        if alt and any(k in alt.lower() for k in BLOCKED_KEYWORDS):
            score -= 50
        width = img.get('width')
        height = img.get('height')
        try:
            if width and int(width) > 200:
                score += 15
            if height and int(height) > 200:
                score += 15
        except:
            pass
        candidates.append((url, score))
    candidates.sort(key=lambda x: x[1], reverse=True)
    for url, score in candidates:
        if score > 20:   
            logger.info(f"📸 Fallback selected image with score {score}: {url}")
            return url
    logger.warning("⚠️ No suitable product image found")
    return None 

# async def aggregate_product(
#     mpn: str = None,
#     upc: str = None,
#     title: str = None,
#     brand: Optional[str] = None,
#     taxonomy: Optional[str] = None,
#     primary_attributes: Optional[List[str]] = None,
#     db: Optional[AsyncSession] = None,
#     project_id:str=None,
#     attribute_chunk:Optional[List[str]]=None
# ) -> Dict:
#     try:
#         if not project_id:
#             raise ValueError("project_id is required for aggregation.")
#         project=await db.get(Project,project_id)
#         if not project:
#             raise ValueError(f"Project {project_id} not found!")
#         use_case = project.use_case
#         if not use_case:
#             raise ValueError(f"No use case defined for project {project_id}.")
#         existing_data={}
#         if 'back filling' in use_case.lower() or 'validation' in use_case.lower():
#             stmt=select(Product).where(Product.product_code==mpn)
#             result=await db.execute(stmt)
#             product=result.scalars().first()
#             if product and product.dynamic_attributes:
#                 for attr in product.dynamic_attributes:
#                     if isinstance(attr, dict):
#                         name=attr.get('name')
#                         value=attr.get('value')
#                         if name and value:
#                             existing_data[name]=value
#         attrs_to_process = attribute_chunk if attribute_chunk is not None else primary_attributes
#         prompt_config = await build_aggregation_prompt(
#             mpn=mpn or "",
#             product_name=title or "",
#             brand=brand,
#             taxonomy=taxonomy,
#             primary_attributes=attrs_to_process,
#             existing_data=existing_data,
#             db=db,
#             use_case=use_case  
#         )
#         logger.info(f"Aggregating {mpn} in '{prompt_config['mode']}' mode")
#         attrs_to_process=attribute_chunk if attribute_chunk is not None else primary_attributes
#         logger.info(f"Expected attributes: {attrs_to_process[:5] if attrs_to_process else []}")
#         pipeline = build_pipeline()
#         result = await pipeline.run(
#             mpn=mpn,
#             upc=upc,
#             title=title,
#             brand=brand,
#             taxonomy=taxonomy,
#             prompt_config=prompt_config
#         )
#         result['mode'] = prompt_config['mode']
#         result['expected_attributes'] = prompt_config['expected_attributes']
#         result['existing_data'] = existing_data 
#         return result

#     except Exception as e:
#         logger.error(f"Aggregation failed for {mpn}: {e}", exc_info=True)
#         return {
#             'status': 'failed',
#             'reason': str(e),
#             'golden_record': {
#                 'attributes': {},
#                 'confidence': 0.0,
#                 'sources_consulted': [] 
                
#             }
#         }

# async def aggregate_product(
#     mpn: str,
#     title: str,
#     brand: Optional[str] = None,
#     taxonomy: Optional[str] = None,
#     primary_attributes: Optional[List[str]] = None,
#     db: Optional[AsyncSession] = None,
#     project_id: str = None,
#     attribute_chunk: Optional[List[str]] = None
# ) -> Dict:
#     """
#     8-Stage Production Pipeline
#     Each stage is isolated and testable
#     """
#     try:
#         logger.info(f"Starting 8-stage aggregation for {mpn}")
#         logger.info("Stage 1: URL Discovery")
#         search_service = SerpApiSearchService(max_results=5)
#         query = f"{brand} {mpn}".strip()
#         urls = await search_service.get_urls(query, mpn=mpn, brand=brand)
#         if not urls:
#             return {
#                 'status': 'failed',
#                 'reason': 'No sources found',
#                 'golden_record': {'attributes': {}}
#             }
#         logger.info(f"Stage 2: Extraction from {len(urls)} sources")
#         download_service = HttpDownloadService(timeout=30)
#         all_extractions = []
#         async def process_url(url):
#             async with _url_semaphore: 
#                 try:
#                     content = await download_service.download(url)
#                     if content['type'] == 'html':
#                             content['text'] = content['raw_bytes'].decode('utf-8', errors='ignore')
#                             logger.info(f" Downloaded HTML from {url} - size: {len(content['text'])} bytes")
#                             from app.aggregation.prompts.extraction_prompts import build_extraction_prompt
#                             prompt_config = build_extraction_prompt(
#                             product_name=title,
#                             mpn=mpn,
#                             brand=brand or "",
#                             taxonomy=taxonomy or "",
#                             primary_attributes=attribute_chunk or primary_attributes or [],
#                             html_content=content['text'])
#                             extraction_result = await call_llm_with_schema(
#                                     prompt=prompt_config['prompt'],
#                                     response_model="ExtractionResponse",
#                                     estimated_tokens=3000 
#                             )
#                             attr_dicts = []
#                             image_url=None
#                             if extraction_result:
#                                 if not extraction_result.product_detected:
#                                     logger.warning(f"LLM decided page is not about {mpn} – product_detected false")
#                                 elif not extraction_result.attributes:
#                                     logger.warning(f"LLM found no attributes for {mpn} (product_detected true)")
#                             if extraction_result and extraction_result.product_detected:
#                                 if hasattr(extraction_result,'image_url'):
#                                     image_url=extraction_result.image_url
#                                 for attr in extraction_result.attributes:
#                                     attr_dicts.append({
#                                 'name': attr.name,
#                                 'value': attr.value,
#                                 'unit': attr.unit if hasattr(attr, 'unit') else None,
#                                 'confidence': attr.confidence if hasattr(attr, 'confidence') else 0.9
#                             })
#                             if not image_url:
#                                 image_url = await extract_fallback_image(content['text'], url)
#                                 if image_url:
#                                     logger.info(f"📸 Fallback extracted image: {image_url}")
#                             from urllib.parse import urlparse
#                             domain = urlparse(url).netloc
#                             return {
#                                 'url': url,
#                                 'domain': domain,
#                                 'attributes': attr_dicts, 
#                                 'image_url': image_url,
#                                 'source_type': 'html'
#                             }
#                     elif content['type'] == 'pdf':   
#                             from app.aggregation.services.pdf_service import PDFExtractionService
#                             pdf_service = PDFExtractionService(max_pages=10)
#                             pdf_text = await pdf_service.extract_text(content['raw_bytes'])
#                             if pdf_text and len(pdf_text.strip()) > 100:
#                                 logger.info(f" Extracted {len(pdf_text)} chars from PDF")
#                                 from app.aggregation.prompts.extraction_prompts import build_pdf_extraction_prompt
#                                 prompt_config = build_pdf_extraction_prompt(
#                                 product_name=title,
#                                 mpn=mpn,
#                                 brand=brand or "",
#                                 taxonomy=taxonomy or "",
#                                 primary_attributes=attribute_chunk or primary_attributes or [],
#                                 pdf_text=pdf_text
#                 )               
#                                 extraction_result = await call_llm_with_schema(
#                                     prompt=prompt_config['prompt'],
#                                     response_model="ExtractionResponse",
#                                     estimated_tokens=4000  
#                                 )
#                                 if extraction_result and extraction_result.product_detected:
#                                     attr_dicts = []
#                                     image_url=None
#                                     if hasattr(extraction_result, 'image_url'):
#                                         image_url = extraction_result.image_url
#                                     for attr in extraction_result.attributes:
#                                         attr_dicts.append({
#                                         'name': attr.name,
#                                         'value': attr.value,
#                                         'unit': getattr(attr, 'unit', None),
#                                         'confidence': getattr(attr, 'confidence', 0.95)
#                                         })
#                                     from urllib.parse import urlparse
#                                     domain = urlparse(url).netloc
#                                     return {
#                                         'url': url,
#                                         'domain': domain,
#                                         'attributes': attr_dicts,
#                                         'image_url': image_url, 
#                                         'source_type': 'pdf'
#                                     }
#                                     return None
#                 except Exception as e:
#                     logger.warning(f"Extraction failed for {url}: {e}")
#                     return None
#         tasks = [process_url(url) for url in urls[:5]]
#         results = await asyncio.gather(*tasks)
#         from asyncio import Semaphore
#         _url_semaphore = Semaphore(2)
#         all_extractions = [r for r in results if r is not None]
#         if not all_extractions:
#             return {
#                 'status': 'failed',
#                 'reason': 'No valid extractions',
#                 'golden_record': {'attributes': {}}
#             }
#         logger.info(f"Stage 2 extracted {sum(len(s['attributes']) for s in all_extractions)} total attributes")
#         def extract_best_image(all_extractions: List[Dict]) -> Optional[str]:
#             """
#             Extract image from highest-trust source
#             Sources are already ranked, so just take first one with image
#             """
#             for source in all_extractions:
#                 img_url = source.get('image_url')
#                 if img_url and isinstance(img_url, str) and img_url.strip():
#                     if img_url.startswith('http'):
#                         logger.info(f"📸 Using image from {source['url']}: {img_url[:80]}")
#                         return img_url.strip()
#             logger.warning("⚠️  No valid image URL found in any source")
#             return None
#         logger.info("Stage 3: Data Cleaning")
#         cleaned_sources = []
#         source_index_map={}
#         combined_for_cleaning={}
#         all_attrs_for_cleaning=[]
#         for idx,source in enumerate(all_extractions):
#             for attr in source['attributes']:
#                 all_attrs_for_cleaning.append({
#                     **attr,
#                     '_source_idx':idx
#                 })
#         cleaning_config = build_cleaning_prompt(all_attrs_for_cleaning)
#         cleaning_result = await call_llm_with_schema(
#                     prompt=cleaning_config['prompt'],
#                     response_model="CleaningResponse",
#                     estimated_tokens=2000
#             )
#         cleaned_sources=[{**source,'attributes':[]}for source in all_extractions]
#         for attr in cleaning_result.cleaned_attributes:
#             src_idx = getattr(attr, '_source_idx', None) or 0
#             if 0 <= src_idx < len(cleaned_sources):
#                 cleaned_sources[src_idx]['attributes'].append(attr)
#         logger.info(f"Stage 3 cleaned {sum(len(s['attributes']) for s in cleaned_sources)} attributes")
#         logger.info("Stage 4: Attribute Unification")
#         all_cleaned_attrs = []
#         for source in cleaned_sources:
#             for attr in source['attributes']:
#                 if hasattr(attr, 'dict'):
#                     all_cleaned_attrs.append(attr.dict())
#                 elif hasattr(attr, '__dict__'):
#                     all_cleaned_attrs.append(attr.__dict__)
#                 elif isinstance(attr, dict):
#                     all_cleaned_attrs.append(attr)
#                 else:
#                     all_cleaned_attrs.append({
#                 'name': getattr(attr, 'name', str(attr)),
#                 'value': getattr(attr, 'value', ''),
#                 'unit': getattr(attr, 'unit', None),
#                 'confidence': getattr(attr, 'confidence', 0.5)
#             })
#         from app.aggregation.prompts.unification_prompts import build_unification_prompt
#         unification_config = build_unification_prompt(
#             cleaned_attributes=all_cleaned_attrs,
#             taxonomy=taxonomy or "General",
#             mpn=mpn
#         )
#         unification_result = await call_llm_with_schema(
#                 prompt=unification_config['prompt'],
#                 response_model="UnificationResponse",
#                 estimated_tokens=1000
#         )
#         unified_sources = apply_unification(cleaned_sources, unification_result.attribute_groups)
#         logger.info(f"Stage 4 unified {sum(len(s['attributes']) for s in unified_sources)} attributes")
#         project = await db.get(Project, project_id) if db and project_id else None
#         use_case = project.use_case.lower() if project and project.use_case else ""
#         validation_conflicts = {}
#         excel_overrides = {}
#         if "back filling" in use_case or "validation" in use_case:
#             logger.info("Stage 5: Excel Validation")
#             from sqlmodel import select
#             from app.models.product import Product
#             stmt = select(Product).where(Product.product_code == mpn)
#             result = await db.execute(stmt)
#             product = result.scalars().first()
#             excel_attrs = {}
#             if product and product.dynamic_attributes:
#                 for attr in product.dynamic_attributes:
#                     if isinstance(attr, dict) and attr.get('name'):
#                         excel_attrs[attr['name']] = attr.get('value', '')
#             web_attrs = {}
#             for source in unified_sources:
#                 for attr in source['attributes']:
#                     web_attrs[attr['name']] = attr['value']
#             from app.aggregation.prompts.validation_prompts import build_validation_prompt
#             validation_config = build_validation_prompt(
#                 excel_attributes=excel_attrs,
#                 web_attributes=web_attrs,
#                 mpn=mpn,
#                 taxonomy=taxonomy or ""
#             )
#             validation_result = await call_llm_with_schema(
#                     prompt=validation_config['prompt'],
#                     response_model="ValidationResponse",
#                     estimated_tokens=1500
#             )
#             if "back filling" in use_case and "validation" not in use_case:
#                 for val in validation_result.validations:
#                     if not val.matches and val.recommendation == "use_web":
#                         excel_overrides[val.attribute_name] = val.web_value
#                         validation_conflicts[val.attribute_name] = val.web_value
#                 for source in unified_sources:
#                     for attr in source['attributes']:
#                         if attr['name'] in excel_overrides:
#                             attr['value'] = excel_overrides[attr['name']]
#             elif "validation" in use_case and "back filling" not in use_case:
#                 logger.info("VALIDATION MODE: Tracking conflicts")
#                 for val in validation_result.validations:
#                     if not val.matches and val.recommendation == "use_web":
#                         validation_conflicts[val.attribute_name] = val.web_value
#             elif "back filling" in use_case and "validation" in use_case:
#                 logger.info("BACKFILL+VALIDATION MODE: Overwriting and tracking")
#                 for val in validation_result.validations:
#                     if not val.matches and val.recommendation == "use_web":
#                         excel_overrides[val.attribute_name] = val.web_value
#                         validation_conflicts[val.attribute_name] = val.web_value
#                 for source in unified_sources:
#                     for attr in source['attributes']:
#                         if attr['name'] in excel_overrides:
#                             attr['value'] = excel_overrides[attr['name']]
#             else:
#                 for val in validation_result.validations:
#                     if not val.matches and val.recommendation == "use_web":
#                         validation_conflicts[val.attribute_name] = val.web_value
#         logger.info("Stage 6: Multi-source Aggregation")
#         aggregation_result = await aggregation.aggregate_attributes(
#             sources_data=unified_sources,
#             primary_attributes=primary_attributes or []
#         )
#         logger.info("Stage 7: Value Standardization")
#         logger.info("Stage 8: Marketing Enrichment")
#         from app.aggregation.prompts.enrichment_prompts import build_enrichment_prompt
#         enrichment_config = build_enrichment_prompt(
#             golden_attributes=aggregation_result['golden_attributes'],
#             product_name=title,
#             brand=brand or "",
#             taxonomy=taxonomy or ""
#         )
#         enrichment_result = await call_llm_with_schema(
#                 prompt=enrichment_config['prompt'],
#                 response_model="EnrichmentResponse",
#                 estimated_tokens=2000,
#                 max_tokens=2000
#             )
#         best_image =  extract_best_image(all_extractions)
#         return {
#             'status': 'success',
#             'golden_record': {
#                 'attributes': {attr['name']: attr for attr in aggregation_result['golden_attributes']},
#                 'short_description': enrichment_result.short_description or "",
#                 'long_description': enrichment_result.long_description,
#                 'features': enrichment_result.features,
#                 'sources_consulted': [s['url'] for s in unified_sources],
#                 'confidence': aggregation_result['consensus_rate']
#             },
#             'validation_conflicts': validation_conflicts,
#              'excel_overrides': excel_overrides,
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
def apply_unification(sources: List[Dict], groups: List) -> List[Dict]:
    """
    Apply unification mapping to rename attributes consistently
    """
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

async def aggregate_product(
    mpn: str,
    title: str,
    brand: Optional[str] = None,
    taxonomy: Optional[str] = None,
    primary_attributes: Optional[List[str]] = None,
    db: Optional[AsyncSession] = None,
    project_id: str = None,
    attribute_chunk: Optional[List[str]] = None
) -> Dict:
    """
    Open-Source Only Aggregation Pipeline
    No OpenAI / No LLM fallback
    """
    try:
        logger.info(f"Starting open-source aggregation for {mpn}")

        # Always use open-source engine
        from app.opensource_aggregation.adapter import opensource_aggregate_product
        
        result = await opensource_aggregate_product(
            mpn=mpn,
            title=title,
            brand=brand,
            taxonomy=taxonomy,
            primary_attributes=primary_attributes,
            db=db,
            project_id=project_id,
            attribute_chunk=attribute_chunk
        )

        logger.info(f"Open-source aggregation completed for {mpn}")
        return result

    except Exception as e:
        logger.error(f"Open-source pipeline failed for {mpn}: {e}", exc_info=True)
        return {
            'status': 'failed',
            'reason': str(e),
            'golden_record': {'attributes': {}}
        }