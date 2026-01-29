from sqlmodel import Field
from datetime import datetime
from app.models.base import UUIDModel
from sqlmodel import Field, Column, JSON
from typing import Optional, Dict,Any

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