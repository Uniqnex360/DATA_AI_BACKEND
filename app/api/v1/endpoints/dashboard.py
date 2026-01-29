import logging
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, func
from app.core.database import get_session
from app.models.product import Product
from app.models.project import Project
from app.schemas.dashboard import DashboardMetricsResponse
logger = logging.getLogger("dashboard_metrics")
router = APIRouter()
@router.get("/metrics", response_model=DashboardMetricsResponse)
async def get_dashboard_metrics(db: AsyncSession = Depends(get_session)):
    try:
        total_projects_stmt = select(func.count(Project.id))
        active_projects_stmt = select(func.count(Project.id)).where(Project.status == "active")
        total_projects_res = await db.execute(total_projects_stmt)
        active_projects_res = await db.execute(active_projects_stmt)
        total_projects = total_projects_res.scalar() or 0
        active_projects = active_projects_res.scalar() or 0
        total_products_stmt = select(func.count(Product.id))
        published_products_stmt = select(func.count(Product.id)).where(Product.published_at.is_not(None))
        total_products_res = await db.execute(total_products_stmt)
        published_products_res = await db.execute(published_products_stmt)
        total_products = total_products_res.scalar() or 0
        published_products = published_products_res.scalar() or 0
        health_stmt = select(func.avg(Product.completeness_score))
        health_res = await db.execute(health_stmt)
        avg_health = health_res.scalar() or 0
        logger.info(f"Dashboard refreshed: {total_products} products, {total_projects} projects")
        return {
            "totalProjects": total_projects,
            "activeProjects": active_projects,
            "totalProducts": total_products,
            "publishedProducts": published_products,
            "catalogHealth": int(avg_health) 
        }
    except Exception as e:
        logger.error(f"CRITICAL: Failed to calculate dashboard metrics: {str(e)}")
        return {
            "totalProjects": 0,
            "activeProjects": 0,
            "totalProducts": 0,
            "publishedProducts": 0,
            "catalogHealth": 0
        }