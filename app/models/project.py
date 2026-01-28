from app.models.base import UUIDModel
from sqlmodel import Field
from typing import Optional
from datetime import datetime

class Project(UUIDModel, table=True):
    __tablename__ = 'catalog_projects'
    name: str = Field(index=True)
    client: Optional[str] = None
    target_platform: str = Field(default="shopify")
    status: str = Field(default="draft") 