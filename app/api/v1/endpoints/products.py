from typing import Any, List
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_session
from app.services.product_service import product_service
from sqlmodel import select
from app.schemas.product import ProductCreate, ProductResponse
import logging
logger=logging.getLogger('products')
router=APIRouter()
@router.get("/", response_model=List[ProductResponse])
async def read_products(
    db: AsyncSession = Depends(get_session), 
    skip: int = 0, 
    limit: int = 100
):
    try:
        statement = select(product_service.model).offset(skip).limit(limit)
        result = await db.execute(statement)
        products = result.scalars().all()

        logger.info(f"DATABASE CHECK: Found {len(products)} rows in product_master")
        
        return products
    except Exception as e:
        logger.error(f"API Error: {str(e)}")
        return []
@router.post('/',response_model=ProductResponse)
async def create_product(*,db:AsyncSession=Depends(get_session),product_in:ProductCreate):
    return await product_service.create(db=db,obj_in=product_in)

@router.post('/{product_code}/enrich')
async def trigger_enrichment(product_code:str,background_tasks:BackgroundTasks,db:AsyncSession=Depends(get_session)):
    product=await product_service.get_by_code(db,product_code)
    if not product:
        raise HTTPException(status_code=404,detail='Product not found')
    background_tasks.add_task(run_enrichment_task,product_code)
    return {"status": "Enrichment started", "product": product.product_name}
    