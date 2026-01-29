from pydantic import BaseModel
from typing import Dict, List, Optional
from uuid import UUID

class SourcePriorityResponse(BaseModel):
    id: UUID
    project_id: str
    source_id: str
    priority_rank: int
    reliability_score: float
    auto_select_enabled: bool
    attribute_priorities: Dict[str, int]

    class Config:
        from_attributes = True 