from sqlmodel import Field, Relationship
from typing import Optional
from .base import UUIDModel
from uuid import UUID

class ProductInventory(UUIDModel,table=True):
    __tablename__='product_inventory'
    product_id:UUID=Field(foreign_key='product_master.id',unique=True,index=True)
    available_quantity:int=Field(default=0)
    inventory_status:str=Field(default='In Stock')
    warehouse_location:Optional[str]=None
    product:Optional['Product']=Relationship(back_populates='inventory')
    