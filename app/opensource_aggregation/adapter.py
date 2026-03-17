from typing import Dict, Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from app.opensource_aggregation.models.schemas import ProductIdentifier
from app.opensource_aggregation.pipeline import OpenSourceAggregationPipeline
from app.core.config import settings

logger = logging.getLogger("os_adapter")


async def opensource_aggregate_product(
    mpn: str,
    title: str,
    brand: Optional[str] = None,
    taxonomy: Optional[str] = None,
    primary_attributes: Optional[List[str]] = None,
    db: Optional[AsyncSession] = None,
    project_id: str = None,
    attribute_chunk: Optional[List[str]] = None
) -> Dict:
    
    try:
        logger.info(f"🆓 Starting open-source aggregation for {mpn} | Brand: {brand}")
        
        product = ProductIdentifier(
            mpn=mpn,
            brand=brand or "",
            title=title or ""
        )

        
        pipeline = OpenSourceAggregationPipeline(serp_api_key=settings.serpapi_key)
        golden = await pipeline.aggregate(product)
        logger.info(f"Golden record attributes count: {len(golden.attributes)}")
        logger.info(f"Golden record attributes keys: {list(golden.attributes.keys())}")

        
        
        if attribute_chunk or primary_attributes:
            requested = attribute_chunk or primary_attributes
            final_attributes = {}
            logger.info(f"Requested primary attributes: {requested}")
            logger.info(f"All extracted attribute keys: {list(golden.attributes.keys())}")

            for req in requested:
                req_norm = req.lower().replace(" ", "_").replace("-", "_").replace("/", "_")
                for name, value in golden.attributes.items():
                    name_norm = name.lower().replace(" ", "_").replace("-", "_").replace("/", "_")
                    if req_norm == name_norm or req_norm in name_norm:
                        final_attributes[req] = value
                        logger.info(f"Matched {req} to {value} (from attribute '{name}')")
                        break

            
            if not final_attributes:
                logger.warning("No primary attributes matched – returning all extracted attributes")
                final_attributes = golden.attributes
        else:
            
            final_attributes = golden.attributes
            logger.info(f"Using all {len(final_attributes)} attributes")
            logger.info(f"FINAL: golden.attributes = {golden.attributes}")
        
        return {
            'status': 'success' if final_attributes else 'partial',
            'golden_record': {
                'attributes': final_attributes,
                'short_description': f"{brand or ''} {title or ''} - {mpn}".strip(),
                'long_description': "",  
                'features': [],
                'sources_consulted': golden.sources_consulted,
                'confidence': golden.confidence_score
            },
            'validation_conflicts': {},
            'excel_overrides': {},
            'image_url': golden.image_url,
            'mode': 'opensource'
        }

    except Exception as e:
        logger.error(f"Open-source aggregation failed for {mpn}: {e}", exc_info=True)
        return {
            'status': 'failed',
            'reason': str(e),
            'golden_record': {'attributes': {}}
        }