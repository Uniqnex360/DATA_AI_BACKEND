from app.models.base import UUIDModel
from sqlmodel import Field

class User(UUIDModel,table=True):
    __tablename__='pim_users'
    email:str=Field(index=True,unique=True)
    hashed_password: str
    full_name: str
    role: str = "viewer" 
    is_active: bool = True