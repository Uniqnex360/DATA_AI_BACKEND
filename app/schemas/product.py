from typing import Optional,Dict
from pydantic import BaseModel 
from uuid import UUID

class ProductBase(BaseModel):
    product_code:str
    product_name:str
    mpn:Optional[str]=None
    brand_name:Optional[str]=None
    category_1: Optional[str] = None
    
class ProductCreate(ProductBase):
    pass

class ProductUpdate(ProductBase):
    product_name: Optional[str] = None
    attributes:Optional[Dict]=None
    enrichment_status:Optional[str]=None

class ProductResponse(BaseModel):
    id: UUID
    product_code: str
    product_name: str
    brand_name: Optional[str] = None
    mpn: Optional[str] = None
    enrichment_status: str = "pending"
    attributes: Dict = {} 
    completeness_score: int = 0

    class Config:
        from_attributes = True 