from typing import Optional
from sqlmodel import SQLModel,Field  
from datetime import datetime
import uuid
class UUIDModel(SQLModel):
    id:uuid.UUID=Field(default_factory=uuid.uuid4,primary_key=True,index=True)
    created_at:datetime=Field(default_factory=datetime.utcnow)
    updated_at:datetime=Field(default_factory=datetime.utcnow)