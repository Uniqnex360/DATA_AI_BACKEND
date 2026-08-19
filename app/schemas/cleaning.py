from pydantic import BaseModel, Field
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

class AttributeInput(BaseModel):
    id: str
    name: str
    value: str
    unit: Optional[str] = None
    source: Optional[str] = None


class ProductContext(BaseModel):
    mpn: Optional[str] = None
    brand: Optional[str] = None
    product_name: Optional[str] = None
    taxonomy: Optional[str] = None


class CleanedAttribute(BaseModel):
    id: Optional[str] = None
    name: str
    original_value: str = ""
    cleaned_value: str
    unit: Optional[str] = None
    cleaning_reason: str = ""
    issue_detected: bool = False
class LLMCleaningResponse(BaseModel):
    cleaned_attributes: List[CleanedAttribute]
    summary: str = Field(description="Brief summary of cleaning actions taken")

