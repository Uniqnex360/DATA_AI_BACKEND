from app.models.base import UUIDModel
from sqlmodel import Field
from typing import Optional,List
from datetime import datetime
from datetime import datetime
from sqlalchemy import Column, JSON

class Project(UUIDModel, table=True):
    __tablename__ = 'catalog_projects'
    name: str = Field(index=True)
    client: Optional[str] = None
    status: str = Field(default="draft") 
    use_case: Optional[str] = Field(default=None)
    products: List["Product"] = Relationship(back_populates="project")