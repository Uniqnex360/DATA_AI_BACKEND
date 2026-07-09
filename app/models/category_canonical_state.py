from __future__ import annotations
from datetime import datetime
from typing import Optional
from uuid import UUID
from sqlalchemy import Column, DateTime, ForeignKey, Index, Text
from sqlalchemy.sql import func
from sqlmodel import Field, SQLModel
class CategoryCanonicalState(SQLModel, table=True):
    __tablename__ = "category_canonical_state"
    __table_args__ = (
        Index("idx_ccs_category_id", "category_id"),
    )
    category_id: UUID = Field(
        sa_column=Column(ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True, nullable=False),
    )
    canonical_fingerprint: str = Field(sa_column=Column(Text, nullable=False))
    alias_generated_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    )
    updated_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    )
    notes: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))