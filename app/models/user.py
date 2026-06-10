from typing import List

from app.models.base import UUIDModel
from sqlmodel import Field, Relationship

class User(UUIDModel,table=True):
    __tablename__='pim_users'
    email:str=Field(index=True,unique=True)
    hashed_password: str
    full_name: str
    role: str = "admin" 
    is_active: bool = True
    projects: List["Project"] = Relationship(back_populates="owner")