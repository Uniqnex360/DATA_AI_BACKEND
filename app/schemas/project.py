
from pydantic import BaseModel, Field
from typing import Optional, List
from uuid import UUID
from datetime import datetime
class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    client: Optional[str] = None
    use_case: Optional[str] = None
    operation_mode:Optional[str]='aggregation'
    status: Optional[str] = "draft"


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    client: Optional[str] = None
    use_case: Optional[str] = None
    operation_mode:Optional[str]='aggregation'
    status: Optional[str] = None


class ProjectResponse(BaseModel):
    id: UUID 
    name: str
    client: Optional[str] = None
    use_case: Optional[str] = None
    operation_mode:Optional[str]=None
    source_status: str | None = None
    created_at: Optional[datetime] = None
    product_count:int=0
    processing_status:str='pending'
    class Config:
        from_attributes = True
