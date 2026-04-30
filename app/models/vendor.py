
from datetime import datetime
from uuid import UUID, uuid4

from sqlmodel import SQLModel, Field
from app.models.base import UUIDModel 
from sqlmodel import Relationship
from typing import Optional, List, Dict, Any

from app.models.industry import Industry

class Vendor(UUIDModel, table=True):
    __tablename__ = "vendor_master"
    vendor_code: str = Field(unique=True, index=True)
    vendor_name: str
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    business_type: Optional[str] = None
    industry_id: Optional[UUID] = Field(default=None, foreign_key="industry_master.id")
    description: Optional[str] = Field(default=None)
    country: Optional[str] = None
    dept1_poc_designation: Optional[str] = None
    dept2_poc_designation: Optional[str] = None
    dept3_poc_designation: Optional[str] = None
    dept4_poc_designation: Optional[str] = None
    dept5_poc_designation: Optional[str] = None
    vendor_logo_url: Optional[str] = None

    dept1_poc_name: Optional[str] = None
    dept1_email: Optional[str] = None
    dept1_phone: Optional[str] = None

    dept2_poc_name: Optional[str] = None
    dept2_email: Optional[str] = None
    dept2_phone: Optional[str] = None

    dept3_poc_name: Optional[str] = None
    dept3_email: Optional[str] = None
    dept3_phone: Optional[str] = None

    dept4_poc_name: Optional[str] = None
    dept4_email: Optional[str] = None
    dept4_phone: Optional[str] = None

    dept5_poc_name: Optional[str] = None
    dept5_email: Optional[str] = None
    dept5_phone: Optional[str] = None

    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    vendor_website: Optional[str] = None

    tax_info: Optional[str] = None
    products: List["Product"] = Relationship(back_populates="vendor")

    industry_obj: Optional[Industry] = Relationship(
        sa_relationship_kwargs={"lazy": "selectin"}
    )
    is_active: bool = True
