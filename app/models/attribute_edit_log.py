from sqlmodel import SQLModel, Field
from typing import Optional
from uuid import UUID, uuid4
from datetime import datetime

class AttributeEditLog(SQLModel, table=True):
    __tablename__ = "attribute_edit_logs"
    
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    product_id: UUID = Field(foreign_key="product_master.id", index=True)
    project_id: Optional[UUID] = Field(default=None, foreign_key="catalog_projects.id", index=True)
    category_name: Optional[str] = Field(default=None, index=True)
    catalog_project_name: Optional[str] = Field(default=None)
    brand_name: Optional[str] = Field(default=None, index=True)
    product_name: Optional[str] = Field(default=None)
    mpn: Optional[str] = Field(default=None)
    attribute_name: str = Field(index=True)
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    edited_by: Optional[str] = Field(default="user")
    algorithm_used: Optional[str] = Field(default=None, index=True)
    edit_source: str = Field(default="manual")
    created_at: datetime = Field(default_factory=datetime.utcnow)