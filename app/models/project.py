from app.models.base import UUIDModel
from typing import Optional,List
from datetime import datetime
from datetime import datetime
from sqlmodel import Field,Column,JSON,Index
from sqlmodel import  Field, Relationship

class Project(UUIDModel, table=True):
    __tablename__ = 'catalog_projects'
    __table_args__ = (
        Index("ix_project_name_trgm", "name", postgresql_using="gin",
              postgresql_ops={"name": "gin_trgm_ops"}),
        Index("ix_project_status_created", "status", "created_at"),
    )
    name: str = Field(index=True)
    client: Optional[str] = None
    aggregation_type:Optional[str] = Field(default=None)
    status: str = Field(default="draft") 
    use_case: Optional[str] = Field(default=None)
    operation_mode: Optional[str] = Field(default="aggregation") 
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow, sa_column_kwargs={"onupdate": datetime.utcnow})
    products: List["Product"] = Relationship(back_populates="project")
