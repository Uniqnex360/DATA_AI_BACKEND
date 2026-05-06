from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, func
from typing import Optional, List
from app.core.database import get_session
from app.models.product import Product
from app.models.attribute_edit_log import AttributeEditLog
from app.models.project import Project
import logging

logger = logging.getLogger("reporting")
router = APIRouter()
@router.get("/data-quality")
async def get_data_quality_report(
    project_id: Optional[str] = None,
    brand_name: Optional[str] = None,
    algorithm: Optional[str] = None,
    db: AsyncSession = Depends(get_session)
):
    try:
        stmt = select(
            Product.project_id,
            Project.name,
            Product.brand_name,
            Product.last_algorithm_used,
            func.count(Product.id).label("total_products"),
            func.avg(Product.data_quality_score).label("avg_quality"),
            func.sum(Product.manual_edit_count).label("total_manual_edits"),
            func.min(Product.data_quality_score).label("min_quality"),
            func.max(Product.data_quality_score).label("max_quality"),
        ).join(Project, Product.project_id == Project.id)
        
        stmt = stmt.where(Project.operation_mode == "cleaning")
        
        if project_id:
            stmt = stmt.where(Product.project_id == project_id)
        if brand_name:
            stmt = stmt.where(Product.brand_name == brand_name)
        if algorithm:
            stmt = stmt.where(Product.last_algorithm_used == algorithm)
        
        stmt = stmt.group_by(
            Product.project_id,
            Project.name,
            Product.brand_name,
            Product.last_algorithm_used,
        ).order_by(func.avg(Product.data_quality_score).asc())
        
        result = await db.execute(stmt)
        rows = result.all()
        
        return [
            {
                "project_id": str(row[0]),
                "project_name": row[1],
                "brand_name": row[2],
                "algorithm_used": row[3],
                "total_products": row[4],
                "avg_quality_score": round(float(row[5] or 0), 2),
                "total_manual_edits": row[6] or 0,
                "min_quality": round(float(row[7] or 0), 2),
                "max_quality": round(float(row[8] or 0), 2),
            }
            for row in rows
        ]
    except Exception as e:
        logger.error(f"Failed to get data quality report: {e}", exc_info=True)
        return []
@router.get("/edit-logs")
async def get_edit_logs(
    project_id: Optional[str] = None,
    product_id: Optional[str] = None,
    brand_name: Optional[str] = None,
    algorithm: Optional[str] = None,
    edit_source: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_session)
):
    try:
        stmt = select(AttributeEditLog)
        
        if project_id:
            stmt = stmt.where(AttributeEditLog.project_id == project_id)
        if product_id:
            stmt = stmt.where(AttributeEditLog.product_id == product_id)
        if brand_name:
            stmt = stmt.where(AttributeEditLog.brand_name == brand_name)
        if algorithm:
            subquery = select(AttributeEditLog.product_id).where(
                AttributeEditLog.algorithm_used == algorithm,
                AttributeEditLog.project_id == Product.project_id
            ).subquery()
            stmt = stmt.where(Product.id.in_(subquery))
        if edit_source:
            stmt = stmt.where(AttributeEditLog.edit_source == edit_source)
        
        stmt = stmt.order_by(AttributeEditLog.created_at.desc()).offset(offset).limit(limit)
        result = await db.execute(stmt)
        logs = result.scalars().all()
        
        return [
            {
                "id": log.id,
                "product_id": log.product_id,
                "product_name": log.product_name,
                "brand_name": log.brand_name,
                "mpn": log.mpn,
                "attribute_name": log.attribute_name,
                "old_value": log.old_value,
                "new_value": log.new_value,
                "algorithm_used": log.algorithm_used,
                "edit_source": log.edit_source,
                "created_at": log.created_at.isoformat() if log.created_at else None,
            }
            for log in logs
        ]
    except Exception as e:
        logger.error(f"Failed to get edit logs: {e}", exc_info=True)
        return []