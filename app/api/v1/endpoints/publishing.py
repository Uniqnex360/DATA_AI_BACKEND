import logging
from typing import List, Dict
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.database import get_session
from app.models.pipeline import PublishTarget
from app.models.product import Product

logger = logging.getLogger("publishing_router")
router = APIRouter()

@router.get("/targets/{project_id}")
async def get_targets(project_id: str, db: AsyncSession = Depends(get_session)):
    try:
        statement = select(PublishTarget).where(PublishTarget.project_id == project_id)
        result = await db.execute(statement)
        return result.scalars().all()
    except Exception as e:
        logger.error(f"Failed to fetch targets for {project_id}: {e}")
        return []

@router.post("/targets")
async def create_target(target_data: PublishTarget, db: AsyncSession = Depends(get_session)):
    
    try:
        db.add(target_data)
        await db.commit()
        await db.refresh(target_data)
        return target_data
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to create target: {e}")
        raise HTTPException(status_code=500, detail="Could not save target")

@router.get("/export/{project_id}")
async def export_catalog_csv(project_id: str, db: AsyncSession = Depends(get_session)):
    try:
        statement = select(Product).where(Product.published_at.is_not(None))
        result = await db.execute(statement)
        products = result.scalars().all()

        if not products:
            return "SKU,Brand,Name\n" 

        csv_lines = ["SKU,Brand,Name,Completeness"]
        for p in products:
            line = f"{p.product_code},{p.brand_name},{p.product_name},{p.completeness_score}%"
            csv_lines.append(line)

        return "\n".join(csv_lines)
    except Exception as e:
        logger.error(f"CSV Export failed: {e}")
        raise HTTPException(status_code=500, detail="Export failed")