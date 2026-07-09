from __future__ import annotations
from datetime import datetime
from typing import Optional
from uuid import UUID
from sqlmodel import Field, SQLModel
from app.models.base import UUIDModel


class CategoryAliasJob(UUIDModel, table=True):
    __tablename__ = "category_alias_jobs"

    category_id: UUID = Field(foreign_key="categories.id", index=True, nullable=False)
    status: str = Field(default="pending", index=True)  # pending | in_progress | completed | failed
    fingerprint: str = Field(description="SHA256 of current canonical set")
    attempts: int = Field(default=0)
    last_error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)