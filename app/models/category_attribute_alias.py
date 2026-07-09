from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.sql import func
from sqlmodel import Field, SQLModel


class CategoryAttributeAlias(SQLModel, table=True):
    __tablename__ = "category_attribute_aliases"
    __table_args__ = (
        UniqueConstraint("category_id", "alias_attribute_id", name="uq_caa_category_alias_attr"),
        Index("idx_caa_category_id", "category_id"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)

    
    category_id: UUID = Field(
        sa_column=Column(ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True, nullable=False),
    )

    
    alias_attribute_id: UUID = Field(
        sa_column=Column(ForeignKey("attribute_master.id", ondelete="CASCADE"), nullable=False)
    )
    canonical_attribute_id: UUID = Field(
        sa_column=Column(ForeignKey("attribute_master.id", ondelete="CASCADE"), nullable=False)
    )

    
    confidence: Optional[float] = Field(sa_column=Column(Float, nullable=True))
    reason: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))

    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    )
    updated_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    )