from pydantic import BaseModel
from typing import List, Optional,Dict

class RunCleaningRequest(BaseModel):
    project_id: str
    llm_provider: str
    product_ids: Optional[List[str]] = None

class BulkUpdateAttributesRequest(BaseModel):
    product_ids: List[str]
    attributes:Dict[str,str]
    llm_provider: Optional[str] = "openai"
    
class ExportSelectedCleaningRequest(BaseModel):
    product_ids: List[str] = []
    project_ids: List[str] = []