from uuid import UUID
from pydantic import BaseModel, Field, validator
from typing import Optional, Dict, Any, List
from datetime import datetime
from app.models.enums import RuleCategory, RuleStatus
class RulePromptBase(BaseModel):
    prompt_name:str
    prompt_text:str
    stage: Optional[str] = None
    description:Optional[str]=None
    priority:int=100
    variables:Optional[List[str]]=None
    status:RuleStatus=RuleStatus.ACTIVE
class RulePromptCreate(RulePromptBase)  :
    pass
class RulePromptUpdate(BaseModel):
    prompt_name:Optional[str]=None
    prompt_text:Optional[str]=None
    description:Optional[str]=None
    priority: Optional[int] = None
    variables:Optional[List[str]]=None
    status:RuleStatus=RuleStatus.ACTIVE
class RulePromptResponse(RulePromptBase):
    id:str
    rule_id:str
    execution_count:int
    last_executed_at:Optional[datetime]
    created_at:datetime
    updated_at:datetime
    class Config:
        from_attributes=True
class BusinessRuleBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    category: RuleCategory
    description: Optional[str] = None
    status: RuleStatus = RuleStatus.ACTIVE
    operation_mode:Optional[str]=None
    use_case:Optional[str]=None
    @validator('title')
    def validate_title(cls,v):
        if not v  or not v.strip():
            raise ValueError('Title is required')
        if len(v.strip())<3:
            raise ValueError("Title must be atleast 3 characters")
        return v.strip()
class BusinessRuleCreate(BusinessRuleBase):
    pass
class BusinessRuleUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    category:Optional[str]=None
    status: Optional[RuleStatus] = None
    operation_mode:Optional[str]=None
    use_case:Optional[str]=None
class BusinessRuleResponse(BusinessRuleBase):
    id: str
    rule_id:str
    prompts:List[RulePromptResponse]=[]
    is_system: bool
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
class CategoryPromptCreate(BaseModel):
    category_id: Optional[UUID] = None 
    stage:Optional[str] = None
    prompt_name: str
    prompt_text: str
    selected_taxonomy: Optional[str] = None 
    description: Optional[str] = None
    variables: Optional[List[str]] = None
    class Config:
        json_encoders = {
            UUID: str
        }
class CategoryPromptResponse(BaseModel):
    id: str
    category_id: Optional[UUID] = None 
    category_name: Optional[str] = None 
    selected_taxonomy: Optional[str] = None 
    stage: Optional[str] = None
    prompt_name: str
    prompt_text: str
    description: Optional[str] = None
    variables: Optional[List[str]] = None
    status: RuleStatus
    created_at: datetime
    updated_at: datetime
    class Config:
        json_encoders = {
            UUID: str
        }
class BrandPromptCreate(BaseModel):
    brand_id:UUID
    stage: Optional[str] = None
    prompt_name: str
    prompt_text: str
    description: Optional[str] = None
    variables: Optional[List[str]] = None
    class Config:
        json_encoders = {
            UUID: str
        }
class BrandPromptResponse(BaseModel):
    id: str
    brand_id:UUID
    brand_name: Optional[str] = None 
    stage: Optional[str] = None
    prompt_name: str
    prompt_text: str
    description: Optional[str] = None
    variables: Optional[List[str]] = None
    status: RuleStatus
    created_at: datetime
    updated_at: datetime
    class Config:
        json_encoders = {
            UUID: str
        }