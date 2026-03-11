from typing import Any, List,Optional,Dict
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_session
from app.services.product_service import product_service
from sqlmodel import select,func
from app.schemas.product import ProductCreate, ProductResponse
import logging
from uuid import UUID
import uuid
from app.models.project import Project
from app.models.product import Product
logger=logging.getLogger('products')
router=APIRouter()
@router.get("/", response_model=Dict[str, Any]) 
async def read_products(
    db: AsyncSession = Depends(get_session), 
    project_id: Optional[UUID]=None,   
    enrichment_status: Optional[str] = None, 
    skip: int = 0, 
    limit: int = 100
):
    try:
        
        statement = select(Product)

        
        if project_id:
            try:
                target_uuid=uuid.UUID(project_id)
                statement = statement.where(Product.project_id == target_uuid)
            except ValueError:
                statement = statement.where(Product.project_id == project_id)
        
        if enrichment_status and enrichment_status != 'all':
            statement = statement.where(product_service.model.enrichment_status == enrichment_status)

        
        count_stmt = select(func.count()).select_from(statement.subquery())
        count_result = await db.execute(count_stmt)
        total = count_result.scalar() or 0

        
        
        statement = statement.order_by(product_service.model.created_at.desc()).offset(skip).limit(limit)
        result = await db.execute(statement)
        products = result.scalars().all()

        
        project_data = None
        if project_id:
            project_data = await db.get(Project, project_id)

        
        return {
            "products": products,
            "total": total,
            "project": project_data, 
            "skip": skip,
            "limit": limit
        }
    except Exception as e:
        logger.error(f"API Error: {str(e)}")
        return {"products": [], "total": 0, "project": None}
    
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
    