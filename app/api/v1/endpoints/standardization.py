from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, delete
from app.core.database import get_session
from app.models.product import Product
from app.models.pipeline import BusinessRule, ReviewItem, StandardizedAttribute
from app.schemas.enrichment import RawValue 
from app.core.config import settings

from app.standardization import standardize_attribute 
import logging
logger = logging.getLogger("standardization_router")
router = APIRouter()
@router.get("/{product_id}")
async def get_standardized(product_id: str, db: AsyncSession = Depends(get_session)):
    try:
        statement = select(StandardizedAttribute).where(StandardizedAttribute.product_id == product_id)
        result = await db.execute(statement)
        return result.scalars().all()
    except Exception as e:
        logger.error(f"UI Fetch Error: {e}")
        return []
    
@router.post("/run/{product_id}")
async def perform_standardization(product_id: str, db: AsyncSession = Depends(get_session)):
    try:
        product = await db.get(Product, product_id)
        if not product or not product.attributes:
            return {"status": "no_data", "message": "No attributes found to standardize"}
        
        rules_stmt = select(BusinessRule).where(BusinessRule.active == True)
        rules_result = await db.execute(rules_stmt)
        active_rules = {r.attribute_name: r.rule_config for r in rules_result.scalars().all()}
        
        await db.execute(delete(StandardizedAttribute).where(StandardizedAttribute.product_id == product_id))
        await db.execute(delete(ReviewItem).where(ReviewItem.product_code == product.product_code))

        for attr_name, attr_value in product.attributes.items():
            raw_input = [RawValue(value=str(attr_value), source="truth_engine")] 
            transformation = standardize_attribute(attr_name, raw_input, active_rules)
            
            if transformation.confidence < settings.HITL_CONFIDENCE_THRESHOLD:
                new_review_task = ReviewItem(
                    product_code=product.product_code,
                    attribute=attr_name,
                    proposed_value=str(transformation.standard_value),
                    confidence=transformation.confidence,
                    reason=f"Low Confidence: {transformation.reason}",
                    derived_from=transformation.derived_from,
                    status="pending"
                )
                db.add(new_review_task)
                logger.warning(f" Flagged {attr_name} for human review (Confidence: {transformation.confidence})")
            
            else:
                new_std = StandardizedAttribute(
                    product_id=product_id,
                    attribute_name=attr_name,
                    standard_value=str(transformation.standard_value),
                    standard_format=transformation.unit if transformation.unit else "string",
                    derived_from=str(transformation.derived_from), 
                    confidence=transformation.confidence,
                    reason=transformation.reason
                )
                db.add(new_std) 

        await db.commit()
        logger.info(f"✓ Standardization complete for product {product_id}")
        return {"status": "success", "attributes_processed": len(product.attributes)}

    except Exception as e:
        await db.rollback()
        logger.error(f"CRITICAL: Standardization engine failed for {product_id}: {str(e)}")
        raise HTTPException(status_code=500, detail="Standardization engine encountered a failure")