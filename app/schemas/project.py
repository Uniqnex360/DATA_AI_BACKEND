
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
    aggregation_type: Optional[str] = None  


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
    completeness_score:Optional[int]=None
    product_count:int=0
    aggregated_count: Optional[int] = 0
    aggregation_type: str | None = None
    processing_status:str='pending'
    class Config:
        from_attributes = True
