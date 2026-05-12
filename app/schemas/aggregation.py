
from pydantic import BaseModel
from typing import Optional,List,Dict
from datetime import datetime
class ProjectStats(BaseModel):
    id: str
    name: str
    client: Optional[str] = None
    status: str
    totalProducts: int
    aggregatedProducts: int
    pendingProducts: int
    failedProducts: int
    aggregationStatus: str  

    class Config:
        from_attributes = True
class BatchExportRequest(BaseModel):
    project_ids:List[str]=[]
    product_ids:List[str]=[]



class AggregationTriggerResponse(BaseModel):
    status: str
    message: str
    job_id: str
    project_id: str
    total_products: int


class AggregatedAttributeValue(BaseModel):
    value: str
    confidence: float
    source_id: str


class AggregatedAttribute(BaseModel):
    id: str
    product_id: str
    attribute_name: str
    has_conflict: bool
    values: List[AggregatedAttributeValue]


class ProductAggregationResponse(BaseModel):
    status: str
    product_id: str
    attributes_count: int
    confidence: float
    message: str

class FinalAttribute(BaseModel):
    name: str
    value: str
    unit: Optional[str] = None
    confidence: float = 0.9
    sources: List[str] = []     
    source_ids: List[str] = []         
    original_values: List[str] = []    

class UnifiedStandardizedResponse(BaseModel):
    attributes: List[FinalAttribute]
    summary: str = ""                    



class AggregateLLMRequest(BaseModel):
    llm_provider: Optional[str] = "openai"
    missing_llm_provider: Optional[str] = None 
    

class AttributeUpdatePayload(BaseModel):
    value: str
    uom: Optional[str] = ""

class UpdateAttributesRequest(BaseModel):
    attributes: Dict[str, AttributeUpdatePayload]
    llm_provider: Optional[str] = "openai" 