from typing import Optional,Dict
from pydantic import BaseModel 
class ProductBase(BaseModel):
    product_code:str
    product_name:str
    mpn:Optional[str]=None
    brand_name:Optional[str]=None
    
class ProductCreate(ProductBase):
    pass

class ProductUpdate(ProductBase):
    attributes:Optional[Dict]=None
    enrichment_status:Optional[str]=None

class ProductResponse(ProductBase):
    id:str 
    enrichment_status:str
    attributes:Dict

    class Config:
        orm_mode=True