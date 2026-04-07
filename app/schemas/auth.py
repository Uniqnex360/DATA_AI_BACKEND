from pydantic import EmailStr
from pydantic import BaseModel, Field

class RegisterRequest(BaseModel):
    full_name:str=Field(min_length=2,max_length=255)
    email:EmailStr
    password:str=Field(min_length=6,max_length=128)