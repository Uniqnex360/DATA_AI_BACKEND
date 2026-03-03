from sqlmodel import SQLModel, Field, Column
from typing import Optional, List
from uuid import UUID, uuid4
from sqlalchemy import JSON
from datetime import datetime

class Brand(SQLModel,table=True):
    __tablename__='brands'
    id:UUID=Field(default_factory=uuid4,primary_key=True)
    name:str=Field(unique=True,index=True)
    normalized_name:str=Field(index=True)
    aliases: List[str] = Field(default=[], sa_column=Column(JSON))
    manufacturer_name:Optional[str]=None
    description:Optional[str]=None
    website:Optional[str]=None
    country_of_origin:Optional[str]=None
    logo_url:Optional[str]=None
    banner_url:Optional[str]=None
    primary_industries: List[str] = Field(default=[], sa_column=Column(JSON))
    is_active:bool=True
    is_verified:bool=False
    product_count:int=0
    created_at:datetime=Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: Optional[str] = None