
from typing import Dict, Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from app.aggregation.pipeline import AggregationPipeline
from sqlmodel import select
from app.aggregation.services.search_service import SerpApiSearchService
from app.aggregation.services.download_service import HttpDownloadService
from app.aggregation.services.extraction_service import (
    ExtractionService, HtmlExtractor, PdfExtractor, PlaywrightExtractor, StructuredDataExtractor)
from app.models.product import Product

from app.models.project import Project

from app.aggregation.services.image_service import ImageService
from app.aggregation.prompt_builder import build_aggregation_prompt
import logging
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
        image_service=ImageService(),
    )
def chunk_attributes(attributes: List[str], chunk_size: int = 10) -> List[List[str]]:
    return [attributes[i:i + chunk_size] for i in range(0, len(attributes), chunk_size)]

async def aggregate_product(
    mpn: str = None,
    upc: str = None,
    title: str = None,
    brand: Optional[str] = None,
    taxonomy: Optional[str] = None,
    primary_attributes: Optional[List[str]] = None,
    db: Optional[AsyncSession] = None,
    project_id:str=None,
    attribute_chunk:Optional[List[str]]=None
) -> Dict:
    try:
        if not project_id:
            raise ValueError("project_id is required for aggregation.")
        project=await db.get(Project,project_id)
        if not project:
            raise ValueError(f"Project {project_id} not found!")
        use_case = project.use_case
        if not use_case:
            raise ValueError(f"No use case defined for project {project_id}.")
        existing_data={}
        if 'back filling' in use_case.lower() or 'validation' in use_case.lower():
            stmt=select(Product).where(Product.product_code==mpn)
            result=await db.execute(stmt)
            product=result.scalars().first()
            if product and product.dynamic_attributes:
                for attr in product.dynamic_attributes:
                    if isinstance(attr, dict):
                        name=attr.get('name')
                        value=attr.get('value')
                        if name and value:
                            existing_data[name]=value
        attrs_to_process = attribute_chunk if attribute_chunk is not None else primary_attributes
        prompt_config = await build_aggregation_prompt(
            mpn=mpn or "",
            product_name=title or "",
            brand=brand,
            taxonomy=taxonomy,
            primary_attributes=attrs_to_process,
            existing_data=existing_data,
            db=db,
            use_case=use_case  
        )
        logger.info(f"Aggregating {mpn} in '{prompt_config['mode']}' mode")
        attrs_to_process=attribute_chunk if attribute_chunk is not None else primary_attributes
        logger.info(f"Expected attributes: {prompt_config['attrs_to_process'][:5]}")
        pipeline = build_pipeline()
        result = await pipeline.run(
            mpn=mpn,
            upc=upc,
            title=title,
            brand=brand,
            taxonomy=taxonomy,
            prompt_config=prompt_config
        )
        result['mode'] = prompt_config['mode']
        result['expected_attributes'] = prompt_config['expected_attributes']
        result['existing_data'] = existing_data 
        return result

    except Exception as e:
        logger.error(f"Aggregation failed for {mpn}: {e}", exc_info=True)
        return {
            'status': 'failed',
            'reason': str(e),
            'golden_record': {
                'attributes': {},
                'confidence': 0.0,
                'sources_consulted': [] 
                
            }
        }
