
from pydantic import BaseModel, Field, model_validator
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
    algorithm_used: Optional[str] = None 

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
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    confidence_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    sources: List[str] = Field(default_factory=list)
    source_ids: List[str] = Field(default_factory=list)
    original_values: List[str] = Field(default_factory=list)
    extraction_algorithm: Optional[str] = None
    merged_from: List[str] = Field(default_factory=list)
    extraction_source: Optional[str] = None
    
    @model_validator(mode='before')
    @classmethod
    def normalize_confidence(cls, data):
        if isinstance(data, dict):
            if 'confidence_score' in data and data['confidence_score'] is not None:
                if 'confidence' not in data or data['confidence'] is None:
                    data['confidence'] = data['confidence_score']
            elif 'confidence' in data and data['confidence'] is not None:
                if 'confidence_score' not in data or data['confidence_score'] is None:
                    data['confidence_score'] = data['confidence']
        return data
    
    class Config:
        extra = "ignore"
        populate_by_name = True

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