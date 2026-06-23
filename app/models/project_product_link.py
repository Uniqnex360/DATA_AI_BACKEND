from datetime import datetime
from uuid import UUID
from sqlmodel import Field, SQLModel


class ProjectProductLink(SQLModel, table=True):
    __tablename__ = "project_product_links"

    project_id: UUID = Field(
        foreign_key="catalog_projects.id",
        primary_key=True
    )
    product_id: UUID = Field(
        foreign_key="product_master.id",
        primary_key=True
    )
    linked_at: datetime = Field(default_factory=datetime.utcnow)
