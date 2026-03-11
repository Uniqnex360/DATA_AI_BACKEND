from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from datetime import datetime
from uuid import UUID

class ExtractionRequest(BaseModel):
    sourceType: str
    content: Optional[str] = None 
    sourceUrl: str
    projectId: Optional[str] = None
    title: Optional[str] = None
    
    
class SourceMetricsResponse(BaseModel):
    avgConfidence: float
    completeness: float
    totalAttributes: int

    class Config:
        from_attributes = True

class SourceResponse(BaseModel):
    id: UUID
    source_url: str
    source_type: str
    status: str
    uploaded_at: datetime
    project_id: Optional[UUID]=None
    
    metadata: Optional[Dict[str, Any]] = Field(default={}, validation_alias="source_metadata")

    class Config:
        from_attributes = True