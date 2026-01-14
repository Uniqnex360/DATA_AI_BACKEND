from app.models.product import Product
from app.schemas.product import ProductCreate,ProductUpdate
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.base import CRUDBase
from sqlmodel import select
from typing import Optional
class CRUDProduct(CRUDBase[Product,ProductCreate,ProductUpdate]):
    async def get_by_code(self,db:AsyncSession,code:str)->Optional[Product]:
        query=select(Product).where(Product.product_code==code)
        result=await db.exec(query)
        return result.one_or_none()    
product_service=CRUDProduct(Product)