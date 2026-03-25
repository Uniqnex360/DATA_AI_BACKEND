from typing import Optional, List
from sqlmodel import SQLModel, Field, Column
from sqlalchemy import JSON
from datetime import datetime
import uuid

class CleaningTask(SQLModel, table=True):
    __tablename__ = "cleaning_tasks"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    status: str = Field(default="pending")
    logs: List[str] = Field(default_factory=list, sa_column=Column(JSON))
    error: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)