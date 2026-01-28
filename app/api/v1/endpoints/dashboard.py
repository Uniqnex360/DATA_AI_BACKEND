from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, func
from app.core.database import get_session
from app.models.product import Product

router = APIRouter()

@router.get("/metrics")
async def get_dashboard_metrics(db: AsyncSession = Depends(get_session)):
    try:
        result = await db.execute(select(func.count(Product.id)))
        total_products = result.scalar()
        
        return {
            "totalProjects": 1,
            "activeProjects": 1,
            "totalProducts": total_products or 0,
            "publishedProducts": 0,
            "catalogHealth": 85
        }
    except Exception:
        return {"totalProjects": 0, "activeProjects": 0, "totalProducts": 0, "publishedProducts": 0, "catalogHealth": 0}