from pydantic import BaseModel
from typing import List, Optional

class RunCleaningRequest(BaseModel):
    project_id: str
    llm_provider: str
    product_ids: Optional[List[str]] = None

class BulkUpdateAttributesRequest(BaseModel):
    product_ids: List[str]
    attribute_name: str
    attribute_value: str