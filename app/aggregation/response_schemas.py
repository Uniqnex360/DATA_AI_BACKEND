from typing import Dict, List, Optional, Literal
from pydantic import BaseModel, Field
from app.cleaning import clean_attribute
"""
Structured output schemas for reliable LLM responses
"""
from typing import Dict, List, Optional, Literal
from pydantic import BaseModel, Field


class ExtractedAttribute(BaseModel):
    name: str = Field(description="Attribute name in Title Case")
    value: str = Field(description="Extracted value")
    unit: Optional[str] = Field(default=None, description="Unit of measurement")
    confidence: float = Field(ge=0.0, le=1.0, description="Extraction confidence")
    source_section: Optional[str] = Field(default=None, description="Which part of document")


class ExtractionResponse(BaseModel):
    attributes: List[ExtractedAttribute]
    product_detected: bool = Field(description="Is this actually the right product?")
    product_type: Optional[str] = Field(default=None, description="Detected product category")
    image_url: Optional[str] = None 


class CleanedAttribute(BaseModel):
    name: str
    value: str
    unit: Optional[str] = None
    confidence: float
    cleaning_applied: List[str] = Field(default_factory=list, description="What cleaning was done")


class CleaningResponse(BaseModel):
    """Stage 3: Cleaning output"""
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
    short_description: str = Field(max_length=200)
    long_description: str = Field(max_length=1000)
    features: List[str] = Field(max_items=10)
    confidence: float