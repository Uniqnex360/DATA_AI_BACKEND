from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_session
from sqlmodel import select
from app.models.product import Product
from app.models.pipeline import AuditTrail, Enrichment

import logging
from datetime import datetime

logger = logging.getLogger("golden_records_router")
router = APIRouter()


@router.get("/")
async def get_golden_records(db: AsyncSession = Depends(get_session)):
    try:
        statement = select(Product).where(Product.enrichment_status == "completed")
        result = await db.execute(statement)
        products = result.scalars().all()

        full_records = []
        for prod in products:
            en_stmt = select(Enrichment).where(Enrichment.product_id == str(prod.id))
            en_result = await db.execute(en_stmt)
            enrichment = en_result.scalars().first()

            record_dict = prod.model_dump()
            record_dict["enrichment"] = enrichment.model_dump() if enrichment else {}
            
            full_records.append(record_dict)

        return full_records
    except Exception as e:
        logger.error(f"Failed to assemble golden records: {e}")
        return []


@router.post("/publish/{product_id}")
async def publish_record(product_id: str, db: AsyncSession = Depends(get_session)):
    try:
        product = await db.get(Product, product_id)
        if not product:
            raise HTTPException(status_code=404, detail="Product not found")

        product.published_at = datetime.utcnow()
        db.add(product)
        
        audit_entry = AuditTrail(
            product_id=product.product_code,
            stage="enrichment",
            attribute_name="golden_record",
            selected_value="published",
            sources_used="Admin User",
            reason="User manually approved and published to production"
        )
        db.add(audit_entry)

        await db.commit()
        return {"status": "success", "published_at": product.published_at}
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    
@router.get("/publishable")
async def get_publishable_records(db: AsyncSession = Depends(get_session)):
    try:
        statement = select(Product).where(
            Product.completeness_score >= 80,
            Product.published_at.is_(None)
        )
        result = await db.execute(statement)
        return result.scalars().all()
    except Exception as e:
        logger.error(f"Failed to fetch publishable records: {e}")
        return []