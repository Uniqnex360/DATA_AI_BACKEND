from sqlmodel import Field
from datetime import datetime
from app.models.base import UUIDModel
from sqlmodel import Field, Column, JSON
from typing import Optional, Dict,Any,List
import uuid

class AuditTrail(UUIDModel,table=True):
    __tablename__='audit_trail'
    product_id:str=Field(index=True)
    attribute_name:str
    selected_value:str
    sources_used:str
    reason:str
    stage:str
    logged_at:datetime=Field(default_factory=datetime.utcnow)

class CleansingIssue(UUIDModel,table=True):
    __tablename__='cleansing_issues'
    product_id:str=Field(index=True)
    attribute_name:str 
    issue_type:str
    details:str
    resolved:bool=Field(default=False)
    detected_at:datetime=Field(default_factory=datetime.utcnow)
class StandarizationAttribute(UUIDModel,table=True):
    __tablename__='standarized_attribute'
    product_id:str=Field(index=True)
    attribute_name:str
    standard_value:str
    standard_format:str
    derived_from:str=Field(default='[]')
    standarized_at:datetime=Field(default_factory=datetime.utcnow)

class BusinessRule(UUIDModel, table=True):
    __tablename__ = 'business_rules'
    rule_id: str = Field(index=True, unique=True)
    attribute_name: str
    rule_type: str  
    rule_config: Dict = Field(default={}, sa_column=Column(JSON))
    active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
class Source(UUIDModel,table=True):
    __tablename__='sources'
    source_type:str
    source_url:str
    project_id: Optional[str] = Field(default=None, index=True)
    source_metadata: Dict = Field(
        default={}, 
        sa_column=Column("metadata", JSON),
        validation_alias="metadata",
        serialization_alias="metadata"
    )
    status:str=Field(default='pending')
    uploaded_at:datetime=Field(default_factory=datetime.utcnow)
    
class ReviewItem(UUIDModel, table=True):
    __tablename__ = 'hitl_review_queue'
    product_code: str = Field(index=True)
    attribute: str
    proposed_value: str
    confidence: float
    reason: str
    derived_from: Any = Field(sa_column=Column(JSON)) 
    status: str = Field(default="pending") 
    reviewer: Optional[str] = None
    overridden_value: Optional[str] = None

class SourcePriority(UUIDModel, table=True):
    __tablename__ = 'source_priority'
    
    project_id: str = Field(index=True)
    source_id: str = Field(index=True)
    priority_rank: int = Field(default=0)
    reliability_score: float = Field(default=0.5)
    auto_select_enabled: bool = Field(default=True)    
    attribute_priorities: Dict = Field(
        default={}, 
        sa_column=Column(JSON)
    )
class Enrichment(UUIDModel, table=True):
    __tablename__ = 'enrichments'
    product_id: str = Field(index=True)
    seo_title: Optional[str] = None
    bullets: List[str] = Field(default=[], sa_column=Column(JSON))
    tags: List[str] = Field(default=[], sa_column=Column(JSON))
    inferred_attributes: Dict = Field(default={}, sa_column=Column(JSON)) 
class StandardizedAttribute(UUIDModel, table=True):
    __tablename__ = 'standardized_attributes'
    product_id: str = Field(index=True)
    attribute_name: str
    standard_value: str
    standard_format: str
    derived_from: str = Field(default="[]") 
    confidence: float = Field(default=0.0)
    reason: str = Field(default="")
class RawExtraction(UUIDModel, table=True):
    __tablename__ = 'raw_extractions'

    source_id: uuid.UUID = Field(foreign_key="sources.id", index=True, nullable=False)
    
    product_keys: Dict = Field(
        default={}, 
        sa_column=Column(JSON),
        description="Identifiers found in the raw content"
    )
    
    raw_attributes: Dict = Field(
        default={}, 
        sa_column=Column(JSON),
        description="Un-standardized key-value pairs from the source"
    )
    
    confidence: float = Field(
        default=0.0, 
        nullable=False,
        description="AI confidence score for this specific extraction"
    )
    
    extracted_at: datetime = Field(
        default_factory=datetime.utcnow,
        nullable=False
    )
class PublishTarget(UUIDModel, table=True):
    __tablename__ = 'publish_targets'
    
    project_id: str = Field(index=True, nullable=False)
    target_name: str
    target_type: str 
    
    connection_config: Dict = Field(default={}, sa_column=Column(JSON))
    
    field_mapping: Dict = Field(default={}, sa_column=Column(JSON))
    
    active: bool = Field(default=True)