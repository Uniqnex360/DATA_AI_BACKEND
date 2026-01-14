from django.contrib.auth import authenticate
from fastapi import APIRouter, Depends, HTTPException, status
from app.core.database import get_session
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.security import OAuth2PasswordRequestForm
from app.services.auth_service import authenticate_user
from typing import Any
from app.core.security import create_access_token, get_password_hash
from datetime import datetime, timedelta
from app.core.config import settings
from app.models.user import User

router=APIRouter()
@router.post('/login/access-token')
async def login_access_token(db:AsyncSession=Depends(get_session),form_data:OAuth2PasswordRequestForm=Depends())->Any:
    user=await authenticate_user(db,form_data.username,form_data.password)
    if not user:
        raise HTTPException(status_code=400,detail='Incorrect email or password')
    access_token_expires=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return {
        'access_token':create_access_token(user.id,expires_delta=access_token_expires),
        'token_type':'bearer'
    }
@router.post('/register')
async def register_user(email:str,password:str,full_name:str,db:AsyncSession=Depends(get_session)):
    user=User(email=email,hashed_password=get_password_hash(password),full_name=full_name,role='admin',is_active=True)
    db.add(user)
    await db.commit()
    return {'msg':"User  created successfully!"}