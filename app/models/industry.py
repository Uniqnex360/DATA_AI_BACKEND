from sqlmodel import SQLModel, Field
from uuid import UUID, uuid4
from typing import Optional
from datetime import datetime

class Industry(SQLModel,table=True):
    __tablename__='industry_master'
    id:UUID=Field(default_factory=uuid4,primary_key=True)
    name:str=Field(unique=True,index=True)
    code:Optional[str]=Field(unique=True,index=True)
    description:Optional[str]=None
    icon:Optional[str]=None
    display_order:int=0 
    is_active:bool=True
    created_at:datetime=Field(default_factory=datetime.utcnow)
    updated_at:datetime=Field(default_factory=datetime.utcnow)
    created_by:Optional[str]=None
    
    