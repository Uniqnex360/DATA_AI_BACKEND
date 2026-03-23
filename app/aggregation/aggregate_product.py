from typing import Dict, Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from app.aggregation.pipeline import AggregationPipeline
from sqlmodel import select
from app.aggregation.prompts.enrichment_prompts import build_enrichment_prompt
from app.aggregation.prompts.extraction_prompts import build_extraction_prompt, build_pdf_extraction_prompt
from app.aggregation.prompts.validation_prompts import build_validation_prompt
from app.aggregation.services.search_service import SerpApiSearchService
from app.aggregation.prompts.cleaning_prompts import build_cleaning_prompt
from app.aggregation.services.download_service import HttpDownloadService
from app.aggregation.services.smart_search import SmartSearchService
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential
from app.aggregation.stages.standardization import BatchStandardizer
from urllib.parse import urlparse
from app.aggregation.services.extraction_service import (
    ExtractionService, HtmlExtractor, PdfExtractor, PlaywrightExtractor, StructuredDataExtractor)
from app.models.product import Product
from app.models.project import Project
import asyncio
from app.aggregation.services.image_service import extract_best_image, extract_best_image_fallback
from app.aggregation.prompt_builder import build_aggregation_prompt
import logging

from app.schemas.aggregation import UnifiedStandardizedResponse
from app.utils.image_validator import validate_image_url
logger = logging.getLogger("aggregate_product")
from app.aggregation.stages import (
    aggregation,
)
_llm_semaphore = asyncio.Semaphore(5)
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
    )
def chunk_attributes(attributes: List[str], chunk_size: int = 10) -> List[List[str]]:
    return [attributes[i:i + chunk_size] for i in range(0, len(attributes), chunk_size)]
from typing import Dict, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from app.aggregation.services.search_service import SerpApiSearchService
from app.aggregation.services.download_service import HttpDownloadService
from app.llm import call_llm_with_schema
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

    # 1. og:image
    meta = soup.find('meta', property='og:image')
    if meta and meta.get('content'):
        url = urljoin(base_url, meta['content'])
        if not is_junk(url): 
            candidates.append((url, 100))  

    # 2. twitter:image
    meta = soup.find('meta', attrs={'name': 'twitter:image'})
    if meta and meta.get('content'):
        url = urljoin(base_url, meta['content'])
        if not is_junk(url): 
            candidates.append((url, 90))

    # 3. Standard img tags
    for img in soup.find_all('img'):
        src = img.get('src')
        if not src: continue
        url = urljoin(base_url, src)
        if is_junk(url): continue 

        alt = img.get('alt', '')
        score = 0
        if any(k in url.lower() for k in ['product', 'main', 'hero']): score += 20
        if alt and any(k in alt.lower() for k in ['product', 'main', 'hero']): score += 10
        
        try:
            width = img.get('width')
            height = img.get('height')
            if width and int(width) > 250: score += 15
            if height and int(height) > 250: score += 15
        except: pass
        candidates.append((url, score))

    candidates.sort(key=lambda x: x[1], reverse=True)
    for url, score in candidates:
        if score > 20:   
            logger.info(f" Fallback selected image with score {score}: {url}")
            return url
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
    
#     try:
#         logger.info(f"Starting 8-stage aggregation for {mpn}")
#         logger.info("Stage 1: URL Discovery")
#         # search_service = SerpApiSearchService(max_results=5)
#         search_service = SmartSearchService(max_results=5)

#         query = title if (mpn in title and brand in title) else f"{brand} {mpn} {title}"
#         query = query.strip()
#         # urls = await search_service.get_urls(query, mpn=mpn, brand=brand,title=title)
#         urls, candidate_images = await search_service.get_urls(query, mpn=mpn, brand=brand, title=title)

#         if not urls:
#             return {
#                 'status': 'failed',
#                 'reason': 'No sources found',
#                 'golden_record': {'attributes': {}}
#             }
#         logger.info(f"Stage 2: Extraction from {len(urls)} sources")
#         download_service = HttpDownloadService(timeout=30)
#         all_extractions = []
#         _url_semaphore = asyncio.Semaphore(2)        
#         async def process_url(url):
#             async with _url_semaphore: 
#                 try:
#                     content = await download_service.download(url)
#                     if content is None:
#                         return None
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
#                             html_content=content['text'],candidate_images=candidate_images)
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
#                                     logger.info(f" Fallback extracted image: {image_url}")
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
            
#             for source in all_extractions:
#                 img_url = source.get('image_url')
#                 if img_url and isinstance(img_url, str) and img_url.strip():
#                     if img_url.startswith('http'):
#                         logger.info(f" Using image from {source['url']}: {img_url[:80]}")
#                         return img_url.strip()
#             logger.warning(" No valid image URL found in any source")
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
#             mpn=mpn,
#             expected_attributes=primary_attributes
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
#         standardizer=BatchStandardizer(target_market='US')
#         standardized_attrs=await standardizer.standardize_attributes(aggregation_result['golden_attributes'])
#         aggregation_result['golden_attributes'] = standardized_attrs
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
#                 max_tokens=4000
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

#Third Case
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
#     Open-Source Only Aggregation Pipeline
#     No OpenAI / No LLM fallback
#     """
#     try:
#         logger.info(f"Starting open-source aggregation for {mpn}")

#         # Always use open-source engine
#         from app.opensource_aggregation.adapter import opensource_aggregate_product
        
#         result = await opensource_aggregate_product(
#             mpn=mpn,
#             title=title,
#             brand=brand,
#             taxonomy=taxonomy,
#             primary_attributes=primary_attributes,
#             db=db,
#             project_id=project_id,
#             attribute_chunk=attribute_chunk
#         )

#         logger.info(f"Open-source aggregation completed for {mpn}")
#         return result

#     except Exception as e:
#         logger.error(f"Open-source pipeline failed for {mpn}: {e}", exc_info=True)
#         return {
#             'status': 'failed',
#             'reason': str(e),
#             'golden_record': {'attributes': {}}
#         }

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
    attribute_chunk: Optional[List[str]] = None,
    existing_excel_attrs:Optional[Dict[str,str]]=None
) -> Dict:
    
    try:
        logger.info(f"Starting aggregation for {mpn}")
        logger.info("Stage 1: URL Discovery")
        search_service = SmartSearchService(max_results=5)

        # Prepare query (same as before)
        query = title if (mpn in title and brand in title) else f"{brand} {mpn} {title}"
        query = query.strip()
        urls, candidate_images = await search_service.get_urls(
            query, mpn=mpn, brand=brand, title=title,sku=sku
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
                        html_text = content['raw_bytes'].decode('utf-8', errors='ignore')
                        logger.info(f"Downloaded HTML from {url} - size: {len(html_text)} bytes")
                        attrs_to_use = primary_attributes or []
                        if attribute_chunk:
                            other_attrs = [a for a in attrs_to_use if a not in attribute_chunk]
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
                            estimated_tokens=3000
                        )
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
                            image_url = await extract_best_image(html_text, url,mpn)
                            if image_url:
                                logger.info(f"Fallback extracted image: {image_url}")

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
                            logger.info(f"Extracted {len(pdf_text)} chars from PDF")
                            attrs_to_use=primary_attributes or []
                            if attribute_chunk:
                                other_attrs=[a for a in attrs_to_use if a not in attribute_chunk]
                                attrs_to_use=attribute_chunk + other_attrs
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

        logger.info(f"Stage 2 extracted {sum(len(s['attributes']) for s in all_extractions)} total attributes")

        # ------------------------------------------------------------
        # Stage 3: Combined Cleaning + Unification + Standardization
        # ------------------------------------------------------------
        logger.info("Stage 3: Combined Cleaning, Unification & Standardization")

        # Collect all attributes with source info
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
        combine_prompt = _build_combined_prompt(raw_attrs_for_combine, brand, mpn, title, taxonomy,existing_excel_attrs=existing_excel_attrs,use_case=use_case)
        # logger.info(f"COMBINED PROMPT:\n{combine_prompt}")
        async for attempt in AsyncRetrying(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10)):
            with attempt:
                combined_result = await call_llm_with_schema(
                    prompt=combine_prompt,
                    response_model="UnifiedStandardizedResponse",  
                    estimated_tokens=3000 + len(raw_attrs_for_combine) * 200,
                    max_tokens=4000
                )
        # logger.info(f"COMBINED RESPONSE:\n{combined_result.model_dump_json(indent=2)}")

        golden_attributes = combined_result.attributes
        valid_source_urls = {source['url'] for source in all_extractions}
        for attr in golden_attributes:
            if hasattr(attr, 'sources') and attr.sources:
                attr.sources=[src for src in attr.sources if src in valid_source_urls]
        logger.info(f"Stage 3 produced {len(golden_attributes)} unified attributes")

        # Optionally, you can still keep per-source images for later selection
        # def extract_best_image(all_extractions: List[Dict]) -> Optional[str]:
        #     for source in all_extractions:
        #         img_url = source.get('image_url')
        #         if img_url and isinstance(img_url, str) and img_url.strip():
        #             if img_url.startswith('http'):
        #                 logger.info(f"Using image from {source['url']}: {img_url[:80]}")
        #                 return img_url.strip()
        #     logger.warning("No valid image URL found in any source")
        #     return None

        # ------------------------------------------------------------
        # Stage 4: Excel Validation (conditional)
        # ------------------------------------------------------------
        
        validation_conflicts = {}
        excel_overrides = {}

        if "back filling" in use_case or "validation" in use_case:
            logger.info("Stage 4: Excel Validation")
            stmt = select(Product).where(Product.product_code == mpn)
            result = await db.execute(stmt)
            product = result.scalars().first()
            excel_attrs = {}
            if product and product.dynamic_attributes:
                for attr in product.dynamic_attributes:
                    if isinstance(attr, dict) and attr.get('name'):
                        excel_attrs[attr['name']] = attr.get('value', '')

            # Build a map of golden attribute values for validation
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
                estimated_tokens=1500
            )

            # Apply backfill/override logic based on use_case
            if "back filling" in use_case and "validation" not in use_case:
                for val in validation_result.validations:
                    if not val.matches and val.recommendation == "use_web":
                        excel_overrides[val.attribute_name] = val.web_value
                        validation_conflicts[val.attribute_name] = val.web_value
                        # Override the golden attribute value if needed
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

        # ------------------------------------------------------------
        # Stage 5: Multi-source Aggregation (simplified – mostly confidence)
        # ------------------------------------------------------------
        logger.info("Stage 5: Multi-source Aggregation")
        # Since we already unified, we just need to compute a consensus rate.
        # For simplicity, we can average confidences or keep the highest.
        if golden_attributes:
            avg_conf = sum(a.confidence for a in golden_attributes) / len(golden_attributes)
        else:
            avg_conf = 0.0

        # If you still need the old aggregation format, convert to list of dicts
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

        # ------------------------------------------------------------
        # Stage 6: Marketing Enrichment
        # ------------------------------------------------------------
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
            estimated_tokens=2000,
            max_tokens=4000
        )
        
        best_image = extract_best_image_fallback(all_extractions)
        if not best_image and candidate_images:
            for candidate in candidate_images:
                is_valid=await validate_image_url(candidate)
                if is_valid:
                    logger.info(f"Fallback to SearXNG image: {candidate}")
                    best_image=candidate
                    break

        return {
            'status': 'success',
            'golden_record': {
                'attributes': {attr['name']: attr for attr in golden_attr_dicts},
                'short_description': enrichment_result.short_description or "",
                'long_description': enrichment_result.long_description,
                'features': enrichment_result.features,
                'sources_consulted': list({s['url'] for s in all_extractions}),  # unique URLs
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
    existing_excel_attrs:Optional[Dict[str,str]]=None,
    use_case:str=None
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
    excel_section=''
    if existing_excel_attrs and any(v.get('value') for v in existing_excel_attrs.values()):
        excel_lines=[]
        for name,val in existing_excel_attrs.items():
            v = val.get('value', '')
            u=val.get('uom','')
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
  For Temporary/Permanent: output ONE status word in Title Case.
  For boolean fields: 1/Y/TRUE → "Yes";  0/N/FALSE → "No".
  Examples:
    "TEMPORARY / PERMANENT"  → "Temporary"
    "PERMANENT"              → "Permanent"
    "Y"                      → "Yes"
    "1"                      → "Yes"

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
{excel_section}
{validation_section }
═══════════════════════════════════════════════════════
INPUT ATTRIBUTES
═══════════════════════════════════════════════════════
{attributes_text}

"""
    return prompt
