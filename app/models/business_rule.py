from sqlmodel import SQLModel, Field, Column, JSON, Relationship
from typing import Optional, Dict, Any, List
from datetime import datetime
from uuid import UUID, uuid4
import enum
import re
from app.models.enums import RuleCategory, RuleStatus


class BusinessRule(SQLModel, table=True):
    __tablename__ = "business_rules"
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    rule_id: str = Field(index=True, unique=True)
    title: str = Field(max_length=255)
    category: RuleCategory
    description: Optional[str] = None
    status: RuleStatus = Field(default=RuleStatus.ACTIVE)
    prompts: List["RulePrompt"] = Relationship(
        back_populates="rule", cascade_delete=True)
    created_by: Optional[str] = None
    updated_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    is_system: bool = Field(default=False)
    operation_mode:Optional[str]=Field(default=None,index=True)
    use_case:Optional[str]=Field(default=None,index=True)

    @staticmethod
    def generate_rule_id(title: str) -> str:
        rule_id = title.lower()
        rule_id = re.sub(r'[^a-z0-9\s]', '', rule_id)
        rule_id = re.sub(r'\s+', '_', rule_id)
        return rule_id


class RulePrompt(SQLModel, table=True):
    __tablename__ = "rule_prompts"
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    rule_id: str = Field(foreign_key="business_rules.id", index=True)
    stage:str=Field(index=True)
    prompt_name: str = Field(max_length=255)
    prompt_text: str
    description: Optional[str] = None
    variables: Optional[List[str]] = Field(
        default=None, sa_column=Column(JSON))
    priority: int = Field(default=100)
    status: RuleStatus = Field(default=RuleStatus.ACTIVE)
    rule: BusinessRule = Relationship(back_populates="prompts")
    execution_count: int = Field(default=0)
    last_executed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class CategoryPrompt(SQLModel, table=True):
    __tablename__ = "category_prompts"
    selected_taxonomy: Optional[str] = Field(default=None)  
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True) 
    category_id: Optional[UUID]  = Field(foreign_key="categories.id", index=True)
    stage : Optional[str] = None
    prompt_name: str = Field(max_length=255)
    prompt_text: str
    description: Optional[str] = None
    variables: Optional[List[str]] = Field(default=None, sa_column=Column(JSON))
    status: RuleStatus = Field(default=RuleStatus.ACTIVE)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class BrandPrompt(SQLModel, table=True):
    __tablename__ = "brand_prompts"
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    brand_id: UUID = Field(foreign_key="brands.id", index=True)
    stage : Optional[str] = None
    prompt_name: str = Field(max_length=255)
    prompt_text: str
    description: Optional[str] = None
    variables: Optional[List[str]] = Field(default=None, sa_column=Column(JSON))
    status: RuleStatus = Field(default=RuleStatus.ACTIVE)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)