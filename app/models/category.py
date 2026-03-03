from sqlmodel import SQLModel, Field, Column
from sqlalchemy import JSON
from uuid import UUID, uuid4
from datetime import datetime
from typing import Optional, List

class Category(SQLModel, table=True):
    __tablename__ = "categories"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(index=True)
    industry_id: UUID = Field(foreign_key="industry_master.id", index=True)
    
    parent_category_id: Optional[UUID] = Field(
        foreign_key="categories.id", 
        default=None,
        index=True
    )
    level: int = Field(default=1)  
    full_path: str = Field(index=True)
    description: Optional[str] = None
    keywords: List[str] = Field(default=[], sa_column=Column(JSON))
    default_attributes: List[dict] = Field(default=[], sa_column=Column(JSON))
    icon: Optional[str] = None
    display_order: int = 0
    is_active: bool = True
    is_leaf: bool = False  
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: Optional[str] = None