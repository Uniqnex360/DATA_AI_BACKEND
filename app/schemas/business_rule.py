
from pydantic import BaseModel, Field, validator
from typing import Optional, Dict, Any, List
from datetime import datetime
from app.models.business_rule import RuleCategory, RuleStatus

class BusinessRuleBase(BaseModel):
    rule_id: str = Field(..., min_length=3, max_length=100)
    title: str = Field(..., min_length=1, max_length=255)
    category: RuleCategory
    description: Optional[str] = None
    prompt: str = Field(..., min_length=10)
    variables: Optional[List[str]] = None
    status: RuleStatus = RuleStatus.ACTIVE
    priority: int = Field(default=100, ge=0, le=1000)
    
    @validator('rule_id')
    def validate_rule_id(cls, v):
        if not v.replace('_', '').isalnum():
            raise ValueError('rule_id must be alphanumeric with underscores')
        return v.lower()
    
    @validator('prompt')
    def validate_prompt(cls, v):
        if len(v.strip()) < 10:
            raise ValueError('Prompt must be at least 10 characters')
        return v.strip()

class BusinessRuleCreate(BusinessRuleBase):
    pass

class BusinessRuleUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    prompt: Optional[str] = None
    variables: Optional[List[str]] = None
    status: Optional[RuleStatus] = None
    priority: Optional[int] = None

class BusinessRuleResponse(BusinessRuleBase):
    id: str
    is_system: bool
    execution_count: int
    last_executed_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    created_by: Optional[str]
    
    class Config:
        from_attributes = True

class BusinessRuleListResponse(BaseModel):
    rules: List[BusinessRuleResponse]
    total: int
    category_counts: Dict[str, int]

class RuleExecuteRequest(BaseModel):
    context: Dict[str, Any]  
    
class RuleExecuteResponse(BaseModel):
    rule_id: str
    status: str
    output: Dict[str, Any]
    execution_time_ms: int
    executed_at: datetime