from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from app.core.database import get_session
from app.models.product import Product
from app.models.pipeline import Enrichment 
import logging
from app.enrichment import enrich_product
logger = logging.getLogger("enrichment_router")
router = APIRouter()
@router.get("/{product_id}")
async def get_enrichment(product_id: str, db: AsyncSession = Depends(get_session)):
    try:
        statement = select(Enrichment).where(Enrichment.product_id == product_id)
        result = await db.execute(statement)
        return result.scalars().first()
    except Exception as e:
        logger.error(f"Failed to fetch enrichment: {e}")
        return None
@router.post("/run/{product_id}")
async def run_enrichment(product_id: str, db: AsyncSession = Depends(get_session)):
    try:
        product = await db.get(Product, product_id)
        if not product:
            raise HTTPException(status_code=404, detail="Product not found")
        enrichment_data = enrich_product(
            brand=product.brand_name or "Generic",
            category=product.category_1 or "Product",
            standardized_attributes=product.attributes
        )
        formatted_use_cases = {
            f"Use Case {i+1}": case for i, case in enumerate(enrichment_data.use_cases)
        }
        statement = select(Enrichment).where(Enrichment.product_id == product_id)
        existing_result = await db.execute(statement)
        existing = existing_result.scalars().first()
        if existing:
            existing.seo_title = enrichment_data.seo_title
            existing.bullets = enrichment_data.bullets
            existing.tags = enrichment_data.tags
            existing.inferred_attributes = formatted_use_cases 
            db.add(existing)
        else:
            new_enrichment = Enrichment(
                product_id=product_id,
                seo_title=enrichment_data.seo_title,
                bullets=enrichment_data.bullets,
                tags=enrichment_data.tags,
                inferred_attributes=formatted_use_cases
            )
            db.add(new_enrichment)
        product.enrichment_status = "completed"
        db.add(product)            
        await db.commit()
        return {"status": "success"}
    except Exception as e:
        await db.rollback()
        logger.error(f"AI Enrichment failed: {str(e)}")
        raise HTTPException(status_code=500, detail="AI content generation failed")
