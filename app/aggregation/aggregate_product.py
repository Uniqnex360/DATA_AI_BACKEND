
from typing import Dict,Optional,List
from sqlalchemy.ext.asyncio import AsyncSession
from app.aggregation.pipeline import AggregationPipeline
from app.aggregation.services.search_service import SerpApiSearchService
from app.aggregation.services.download_service import HttpDownloadService
from app.aggregation.services.extraction_service import (ExtractionService, HtmlExtractor, PdfExtractor, PlaywrightExtractor,StructuredDataExtractor)
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


async def aggregate_product(
    mpn: str = None,
    upc: str = None,
    title: str = None,
    brand: Optional[str] = None,      
    taxonomy: Optional[str] = None,  
    primary_attributes: Optional[List[str]] = None,
    db: Optional[AsyncSession] = None
) -> Dict:
    try:
        prompt_config = await build_aggregation_prompt(
            mpn=mpn or "",
            product_name=title or "",
            brand=brand,
            taxonomy=taxonomy,
            primary_attributes=primary_attributes,
            db=db
        )
        logger.info(f"Aggregating {mpn} in '{prompt_config['mode']}' mode")
        logger.info(f"Expected attributes: {prompt_config['expected_attributes'][:5]}")
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
        return result
        
    except Exception as e:
        logger.error(f"Aggregation failed for {mpn}: {e}", exc_info=True)
        return {
            'status': 'failed',
            'reason': str(e),
            'golden_record': {
                'attributes': {},
                'confidence': 0.0
            }
        }