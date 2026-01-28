from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_session
from sqlmodel import select
from app.models.product import Product
import logging

logger = logging.getLogger("golden_records_router")
router = APIRouter()

@router.get("/")
async def get_golden_records(db: AsyncSession = Depends(get_session)):
    try:
        statement = select(Product).where(Product.enrichment_status == "completed")
        result = await db.execute(statement)
        records = result.scalars().all()
        return records
    except Exception as e:
        logger.error(f"Golden Records Fetch Error: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fetch golden records")