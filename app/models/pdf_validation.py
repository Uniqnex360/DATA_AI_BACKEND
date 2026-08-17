from typing import Optional

from app.models.base import UUIDModel
from sqlmodel import Field
from uuid import UUID, uuid4
from datetime import datetime

from app.utils.timezone import now_ist

class PdfValidation(UUIDModel,table=True):
    id:UUID=Field(default_factory=uuid4,primary_key=True)
    product_code: str  
    product_id: Optional[UUID] = Field(default=None, foreign_key='product_master.id')
    project_id:UUID
    pdf_url:str
    source_page_url:str
    status:str='pending'
    created_at:datetime=Field(default_factory=now_ist)
    resolved_at:Optional[datetime]=None
    resolved_by:Optional[UUID]=None
