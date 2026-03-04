from sqlmodel import SQLModel, Field, Column, JSON
from typing import Optional, Dict, Any, List
from datetime import datetime
from uuid import uuid4, UUID
import enum

class RuleCategory(str,enum.Enum):
    ENRICHMENT='enrichment'
    AGGREGATION='aggregation'
    VALIDATION='validation'
    CLEANSING='cleansing'

class RuleStatus(str,enum.Enum):
    ACTIVE='active'
    INACTIVE='inactive'
    DRAFT='draft'

class BusinessRule(SQLModel,table=True):
    __tablename__='business_rules'
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    rule_id:str=Field(index=True,unique=True)
    title:str=Field(max_length=255)
    category:RuleCategory
    description:Optional[str]=None
    prompt:str=Field(sa_column=Column(JSON))
    variables:Optional[Dict[str,Any]]=Field(default=None,sa_column=Column(JSON))
    status: RuleStatus = Field(default=RuleStatus.ACTIVE)
    priority:int=Field(default=100)
    created_by:Optional[str]=None
    updated_by:Optional[str]=None
    created_at:datetime=Field(default_factory=datetime.utcnow)
    updated_at:datetime=Field(default_factory=datetime.utcnow)
    execution_count: int = Field(default=0)
    last_executed_at: Optional[datetime] = None
    is_system: bool = Field(default=False)
    
    class Config:
        json_schema_extra={
            'example':{
                'rule_id':'enrich_product_v1',
                'title':'Enrich Product',
                'category':'enrichment',
                'description':"Generate  SEO from attributes",
                'prompt':'You are an expert copywriter...',
                'variables':['brand','category','attributes'],
                'status':'active',
                'priority':100
            }
        }
    
class RuleExecutionLog(SQLModel,table=True):
    __tablename__='rule_execution_log'
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    rule_id:str=Field(index=True,unique=True)
    product_id:Optional[str]=Field(index=True)
    input_data:Optional[Dict[str,Any]]=Field(sa_column=Column(JSON))
    output_data:Optional[Dict[str,Any]]=Field(sa_column=Column(JSON))
    status:str=Field(default='success')
    error_message:Optional[str]=None
    execution_time_ms:Optional[str]=None
    executed_at:datetime=Field(default_factory=datetime.utcnow)
    class Config:
        json_schema_extra={
            'example':{
                'rule_id':'uuid',
                'product_id':'uuid',
                'status':'success',
                'execution_time_ms':1250
            }
        }