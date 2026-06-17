from pydantic import BaseModel, EmailStr
from typing import Optional
from uuid import UUID
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str
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
    user: UserResponse
