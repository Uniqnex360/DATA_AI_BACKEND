from typing import Dict, List, Optional, Literal
from pydantic import BaseModel, Field, model_validator
from typing import Dict, List, Optional, Literal

class ExtractedAttribute(BaseModel):
    name: str = Field(description="Attribute name in Title Case")
    value: Optional[str] = Field(default=None, description="Extracted value")  
    unit: Optional[str] = Field(default=None, description="Unit of measurement")
    confidence: float = Field(default=0.9, ge=0.0, le=1.0, description="Extraction confidence")
    confidence_score: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="Alias for confidence")
    source_section: Optional[str] = Field(default=None, description="Which part of document")
    extraction_algorithm: Optional[str] = Field(default="Algo 1", description="Which algorithm extracted this")
    extraction_source: Optional[str] = Field(default="html", description="html or pdf")
    sources: List[str] = Field(default_factory=list, description="Source URLs")
    
    @model_validator(mode='before')
    @classmethod
    def normalize_confidence(cls, data):
        if isinstance(data, dict):
            if 'confidence_score' in data and 'confidence' not in data:
                data['confidence'] = data['confidence_score']
            elif 'confidence' in data and 'confidence_score' not in data:
                data['confidence_score'] = data['confidence']
        return data
    
    class Config:
        extra = "ignore"  # Ignore unknown fields like 'merged_from'
        populate_by_name = True
class ExtractionResponse(BaseModel):
    attributes: List[ExtractedAttribute]
    product_detected: bool = Field(description="Is this actually the right product?")
    product_type: Optional[str] = Field(default=None, description="Detected product category")
    image_url: Optional[str] = None 
    description: Optional[str] = None 
    long_description: Optional[str] = None
    features: Optional[List[str]] = None
class CanonicalMatchItem(BaseModel):
    raw_name: str = Field(..., description="The newly scraped attribute name being evaluated")
    matched_canonical: Optional[str] = Field(
        None,
        description="Exact name of the matching DB canonical attribute, or null if no match"
    )
    confidence: float = Field(..., ge=0.0, le=1.0)
 
 
class BatchCanonicalMatchResponse(BaseModel):
    matches: List[CanonicalMatchItem] = Field(default_factory=list)
 


class CleanedAttribute(BaseModel):
    name: str
    value: str
    unit: Optional[str] = None
    confidence: float
    cleaning_applied: List[str] = Field(default_factory=list, description="What cleaning was done")
    _source_idx:Optional[int]=None


class CleaningResponse(BaseModel):
    cleaned_attributes: List[CleanedAttribute]
    removed_count: int = Field(description="How many invalid values removed")
    issues_found: List[str] = Field(default_factory=list)


class AttributeGroup(BaseModel):
    canonical_name: str = Field(description="The standard name to use")
    grouped_attributes: List[str] = Field(description="Original names that mean the same thing")
    reasoning: str = Field(description="Why these were grouped together")


class UnificationResponse(BaseModel):
    attribute_groups: List[AttributeGroup]
    product_category: str = Field(description="Detected category for context")


class ValidationResult(BaseModel):
    attribute_name: str
    excel_value: str
    web_value: str
    matches: bool
    confidence: float
    recommendation: Literal["keep_excel", "use_web", "manual_review"]
    reasoning: str


class ValidationResponse(BaseModel):
    validations: List[ValidationResult]
    match_rate: float = Field(description="Percentage of exact matches")


class AggregatedAttribute(BaseModel):
    name: str
    value: str
    unit: Optional[str] = None
    confidence: float
    source_count: int = Field(description="How many sources agree")
    sources: List[str] = Field(description="Which sources provided this")


class AggregationResponse(BaseModel):
    golden_attributes: List[AggregatedAttribute]
    total_sources: int
    consensus_rate: float = Field(description="% attributes with multi-source agreement")


class EnrichmentResponse(BaseModel):
    short_description: str = Field(max_length=500)
    long_description: str = Field(max_length=5000)
    features: List[str] = Field(max_items=10)
    confidence: float
class StandardizedAttribute(BaseModel):
    name: str
    value: str
    unit: Optional[str] = None
    conversion_applied: Optional[str] = Field(
        default=None,
        description="What was changed, e.g., 'Converted 24 inches to 24 in' or 'Averaged range 10-20 to 15'"
    )
class StandardizationResponse(BaseModel):
    standardized_attributes: List[StandardizedAttribute] = Field(
        description="Standardized attributes in the SAME ORDER as input"
    )
    market: str = Field(description="Target market (US/EU)")
    
    class Config:
        json_schema_extra = {
            "example": {
                "standardized_attributes": [
                    {
                        "name": "Length",
                        "value": "24",
                        "unit": "in",
                        "conversion_applied": "Removed unit text from value"
                    },
                    {
                        "name": "Weight",
                        "value": "23.15",
                        "unit": "lb",
                        "conversion_applied": "Converted 10.5 kg to 23.15 lb"
                    }
                ],
                "market": "US"
            }
        }
