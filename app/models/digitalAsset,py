from sqlmodel import SQLModel, Field, Relationship
from typing import Optional
from datetime import datetime
import uuid
from app.models.product import Product

class DigitalAsset(SQLModel, table=True):
    __tablename__ = "digital_assets"
    
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    file_name: str
    file_url: str
    file_type: str  
    file_size: int
    public_id: Optional[str] = None
    is_archived:bool=False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    product_id: Optional[uuid.UUID] = Field(default=None, foreign_key="product_master.id")
    product: Optional["Product"] = Relationship(back_populates="digital_assets")
    category: Optional[str]
    brand: Optional[str]
    mpn: Optional[str]
