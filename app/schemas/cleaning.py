from pydantic import BaseModel
from typing import List, Optional

class RunCleaningRequest(BaseModel):
    project_id: str
    llm_provider: str
    product_ids: Optional[List[str]] = None