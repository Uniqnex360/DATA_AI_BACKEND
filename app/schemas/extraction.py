from pydantic import BaseModel
from typing import Optional

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