import logging
from typing import List, Dict, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, func
from app.core.database import get_session
from app.models.pipeline import ReviewItem
from app.models.product import Product
logger = logging.getLogger("hitl_router")
router = APIRouter()
@router.get("/pending")
async def get_pending_items(
    status: Optional[str] = None, 
    db: AsyncSession = Depends(get_session)
):
    try:
        statement = select(ReviewItem)
        if status and status != 'all':
            statement = statement.where(ReviewItem.status == status)
        result = await db.execute(statement)
        return result.scalars().all() 
    except Exception as e:
        logger.error(f"Failed to fetch HITL items: {e}")
        return [] 
@router.get("/stats/{project_id}")
async def get_hitl_stats(project_id: str, db: AsyncSession = Depends(get_session)):
    try:
        total = await db.execute(select(func.count(ReviewItem.id)))
        pending = await db.execute(select(func.count(ReviewItem.id)).where(ReviewItem.status == "pending"))
        approved = await db.execute(select(func.count(ReviewItem.id)).where(ReviewItem.status == "approved"))
        rejected = await db.execute(select(func.count(ReviewItem.id)).where(ReviewItem.status == "rejected"))
        return {
            "total": total.scalar() or 0,
            "pending": pending.scalar() or 0,
            "approved": approved.scalar() or 0,
            "rejected": rejected.scalar() or 0,
            "in_review": 0
        }
    except Exception as e:
        logger.error(f"Failed to fetch HITL stats: {e}")
        return {"total": 0, "pending": 0, "approved": 0, "rejected": 0, "in_review": 0}
@router.post("/approve")
async def approve_item(
    queue_id: str, 
    db: AsyncSession = Depends(get_session)
):
    try:
        item = await db.get(ReviewItem, queue_id)
        if not item:
            raise HTTPException(status_code=404, detail="Review task not found")
        item.status = "approved"
        item.reviewer = "Human Validator" 
        stmt = select(Product).where(Product.product_code == item.product_code)
        result = await db.execute(stmt)
        product = result.scalars().first()
        if product:
            new_attrs = dict(product.attributes)
            new_attrs[item.attribute] = item.proposed_value
            product.attributes = new_attrs
            db.add(product)
        await db.commit()
        logger.info(f"✓ Approved attribute '{item.attribute}' for product '{item.product_code}'")
        return {"status": "success", "id": queue_id}
    except Exception as e:
        await db.rollback()
        logger.error(f"Approval failed for {queue_id}: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to process approval")  
    
@router.post("/override")
async def override_item(
    product_key: str, 
    attribute: str, 
    new_value: str, 
    reviewer: str, 
    db: AsyncSession = Depends(get_session)
):
    try:
        statement = select(ReviewItem).where(
            ReviewItem.product_code == product_key, 
            ReviewItem.attribute == attribute
        )
        result = await db.execute(statement)
        item = result.scalars().first()
        if not item:
            raise HTTPException(status_code=404, detail="Item not found")
        item.status = "approved"
        item.overridden_value = new_value
        item.reviewer = reviewer
        prod_statement = select(Product).where(Product.product_code == product_key)
        prod_result = await db.execute(prod_statement)
        product = prod_result.scalars().first()
        if product:
            new_attrs = dict(product.attributes)
            new_attrs[attribute] = new_value 
            product.attributes = new_attrs
            db.add(product)
        await db.commit()
        return {"status": "success", "value": new_value}
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
@router.post("/reject")
async def reject_item(
    product_key: str, 
    attribute: str, 
    reviewer: str, 
    db: AsyncSession = Depends(get_session)
):
    try:
        statement = select(ReviewItem).where(
            ReviewItem.product_code == product_key, 
            ReviewItem.attribute == attribute
        )
        result = await db.execute(statement)
        item = result.scalars().first()
        if item:
            item.status = "rejected"
            item.reviewer = reviewer
            await db.commit()
            return {"status": "rejected"}
        raise HTTPException(status_code=404, detail="Item not found")
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))