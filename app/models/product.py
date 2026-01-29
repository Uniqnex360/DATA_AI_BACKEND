from app.models.base import UUIDModel
from typing import Optional,Dict
from sqlmodel import Field,Column,JSON
from datetime import datetime

class Product(UUIDModel,table=True):
    __tablename__='product_master'
    product_code:str=Field(index=True,unique=True)
    product_name:str
    brand_name:Optional[str]=None
    brand_code:Optional[str]=None
    category_1:Optional[str]=None
    vendor_name:Optional[str]=None
    mpn:Optional[str]=Field(index=True,default=None)
    published_at: Optional[datetime] = Field(default=None, nullable=True)
    gtin:Optional[str]=None
    upc:Optional[str]=None
    description:Optional[str]=None
    image_url_1:Optional[str]=None
    enrichment_status:str=Field(default='pending')
    completeness_score:int=Field(default=0)
    attributes:Dict=Field(default={},sa_column=Column(JSON))