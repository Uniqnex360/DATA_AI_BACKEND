from app.models.base import UUIDModel
from typing import Optional,List
from datetime import datetime
from datetime import datetime
from sqlalchemy import Column, JSON
from sqlmodel import  Field, Relationship

class Project(UUIDModel, table=True):
    __tablename__ = 'catalog_projects'
    name: str = Field(index=True)
    client: Optional[str] = None
    aggregation_type:Optional[str] = Field(default=None)
    status: str = Field(default="draft") 
    use_case: Optional[str] = Field(default=None)
    operation_mode: Optional[str] = Field(default="aggregation") 
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow, sa_column_kwargs={"onupdate": datetime.utcnow})
    products: List["Product"] = Relationship(back_populates="project")
