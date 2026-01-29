from pydantic import BaseModel
from typing import Optional

class ExtractionRequest(BaseModel):
    sourceType: str
    content: str
    sourceUrl: str
    projectId: Optional[str] = None 


class SourceMetricsResponse(BaseModel):
    avgConfidence: float
    completeness: float
    totalAttributes: int

    class Config:
        from_attributes = True