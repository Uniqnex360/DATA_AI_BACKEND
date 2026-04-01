from app.models.base import UUIDModel
from typing import Optional,Dict,List,Any
from sqlmodel import Field,Column,JSON
from datetime import datetime
from typing import List
from uuid import UUID, uuid4
from sqlmodel import Relationship
from app.models.vendor import Vendor
from app.models.product_attribute_link import ProductAttributeLinkModel, ProductAttributeValueLinkModel
class Product(UUIDModel,table=True):
    __tablename__='product_master'
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    product_code:str=Field(index=True)
    product_name:str
    sku: Optional[str] = Field(index=True)
    brand_id: Optional[UUID] = Field(foreign_key="brands.id", index=True)
    brand_name: Optional[str] = Field(default="") 
    brand_code: Optional[str] = Field(default="")
    industry_id: Optional[UUID] = Field(foreign_key="industry_master.id",index=True)
    industry_name: Optional[str] = Field(default=None) 
    industry_code: Optional[str] = Field(default=None)
    taxonomy: Optional[str] = Field(default=None, index=True) 
    category_1:Optional[str]=None
    category_2: Optional[str] = Field(default=None)
    category_3: Optional[str] = Field(default=None)
    category_4: Optional[str] = Field(default=None)
    category_5: Optional[str] = Field(default=None)
    category_6: Optional[str] = Field(default=None)
    category_7: Optional[str] = Field(default=None)
    category_8: Optional[str] = Field(default=None)
    base_price: Optional[float] = Field(default=0.0) 
    currency: Optional[str] = Field(default="USD")  
    sale_price: Optional[float] = Field(default=0.0)
    list_price: Optional[float] = Field(default=0.0)
    
    warranty: Optional[str] = Field(default=None)   
    weight: Optional[str] = Field(default=None)     
    weight_unit: Optional[str] = Field(default=None)
    length: Optional[str] = Field(default=None)     
    width: Optional[str] = Field(default=None)      
    height: Optional[str] = Field(default=None)     
    dimension_unit: Optional[str] = Field(default=None)
    vendor_id: Optional[UUID] = Field(
        default=None,
        foreign_key="vendor_master.id",
        index=True
    )
    vendor_name: Optional[str] = Field(default="")
    vendor: Optional[Vendor] = Relationship(back_populates="products")
    mpn:Optional[str]=Field(index=True,default=None)
    model_numer:Optional[str]=Field(index=True,default=None)
    attribute_objs: List["Attribute"] = Relationship(
        back_populates="products", 
        link_model=ProductAttributeLinkModel
    )
    validation_conflicts: Optional[Dict[str, Any]] = Field(
        default=None,
        sa_column=Column(JSON)
    )
    attribute_values: List["AttributeValue"] = Relationship(
        back_populates="products", link_model=ProductAttributeValueLinkModel
    )
    sources_consulted: Optional[List[str]] = Field(default=None, sa_column=Column(JSON))
    published_at: Optional[datetime] = Field(default=None, nullable=True)
    source_url: Optional[str] = Field(default=None) 
    project_id: Optional[UUID] = Field(default=None, index=True, foreign_key="catalog_projects.id")
    project: Optional["Project"] = Relationship(back_populates="products")
    description:Optional[str]=None
    image_url_1:Optional[str]=None
    enrichment_status:str=Field(default='pending')
    completeness_score:int=Field(default=0)
    attributes:Dict=Field(default={},sa_column=Column(JSON))
    product_type:Optional[str]=None
    parent_sku:Optional[str]=None
    gtin:Optional[str]=None
    ean:Optional[str]=None
    upc:Optional[str]=None
    unspc:Optional[str]=None
    lifecycle_stage:Optional[str]=None
    launch_date:Optional[str]=None
    discontinue_status:Optional[str]=None
    dynamic_attributes: List[dict] = Field(default=[], sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    short_description:Optional[str]=None
    long_description:Optional[str]=None
    features:Optional[List]=Field(default=None,sa_column=Column(JSON))
    meta_title: Optional[str] = None
    search_keywords: Optional[str] = None
    meta_description: Optional[str] = None
    certification: Optional[str] = None
    safety_standard: Optional[str] = None
    hazardous_material: Optional[str] = None
    prop65_warning: Optional[str] = None
    workflow_stage: str = Field(default="aggregation", index=True)
    needs_enrichment: bool = Field(default=False, index=True)
    ready_for_export: bool = Field(default=False, index=True)
    routed_to_enrichment_at: Optional[datetime] = Field(default=None, nullable=True)
