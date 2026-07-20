from pydantic import BaseModel, EmailStr, validator
from typing import Optional
from uuid import UUID
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    @validator('email')
    def validate_email(cls, v):
        import re
        if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', v):
            raise ValueError('Invalid email format')
        return v.lower()
class UserPublic(BaseModel):
    id: UUID
    email: str
    full_name: str
    role: str
class UserResponse(BaseModel):
    id: UUID
    email: EmailStr
    full_name: str
    role: str
    is_active: bool
    class Config:
        from_attributes = True
class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserPublic
