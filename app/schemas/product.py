from typing import Any, List, Optional,Dict
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
class ImageAssetResponse(BaseModel):
    image_url:str
    source_page_url:Optional[str]=None
    source_type:Optional[str]=None
    is_primary:bool=False
class ProductResponse(BaseModel):
    id: UUID
    product_code: str
    product_name: str
    brand_name: Optional[str] = None
    mpn: Optional[str] = None
    enrichment_status: str = "pending"
    attributes: Optional[Dict[str, Any]] = None
    completeness_score: float = 0.0
    image_url_1:Optional[List[ImageAssetResponse]]=None

    class Config:
        from_attributes = True 