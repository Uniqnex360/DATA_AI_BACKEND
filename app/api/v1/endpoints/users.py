from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from app.auth.dependencies import require_roles
from app.core.database import get_session
from sqlmodel import select
from app.models.user import User
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from typing import List, Optional
from app.core.database import get_session
from app.models.user import User
from app.auth.dependencies import get_current_user, require_roles
from app.auth.security import get_password_hash
from pydantic import BaseModel, EmailStr
import logging

from pydantic import BaseModel, EmailStr

logger = logging.getLogger("user_router")
router = APIRouter()

class CreateUserRequest(BaseModel):
    email: EmailStr
    full_name: str
    password: str
    role: str = "user"

class UpdateUserRequest(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None

class ResetPasswordRequest(BaseModel):
    password: str

@router.get("/")
async def list_users(db: AsyncSession = Depends(get_session)):
    try:
        statement = select(User).order_by(User.created_at.desc())
        result = await db.execute(statement)
        users = result.scalars().all()
        return users
    except Exception as e:
        logger.error(f"User List Error: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to retrieve users")

@router.get("/", dependencies=[Depends(require_roles("admin"))])
async def list_users(db: AsyncSession = Depends(get_session)):
    try:
        result = await db.execute(select(User).order_by(User.created_at.desc()))
        users = result.scalars().all()
        return [
            {
                "id": str(u.id),
                "email": u.email,
                "full_name": u.full_name,
                "role": u.role,
                "is_active": u.is_active
            }
            for u in users
        ]
    except Exception as e:
        logger.error(f"Failed to list users: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch users"
        )

@router.post("/", dependencies=[Depends(require_roles("admin"))])
async def create_user(
    payload: CreateUserRequest,
    db: AsyncSession = Depends(get_session)
):
    try:
        existing = (
            await db.execute(select(User).where(User.email == payload.email))
        ).scalar_one_or_none()

        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email already exists"
            )

        user = User(
            email=payload.email,
            full_name=payload.full_name,
            hashed_password=get_password_hash(payload.password),
            role=payload.role,
            is_active=True
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

        logger.info(f"User created: {user.email} with role {user.role}")
        return {
            "id": str(user.id),
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role,
            "is_active": user.is_active
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to create user: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create user"
        )

@router.patch("/{user_id}", dependencies=[Depends(require_roles("admin"))])
async def update_user(
    user_id: str,
    payload: UpdateUserRequest,
    db: AsyncSession = Depends(get_session)
):
    try:
        user = await db.get(User, user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        if payload.full_name is not None:
            user.full_name = payload.full_name
        if payload.role is not None:
            user.role = payload.role
        if payload.is_active is not None:
            user.is_active = payload.is_active

        db.add(user)
        await db.commit()

        logger.info(f"User updated: {user.email}")
        return {
            "id": str(user.id),
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role,
            "is_active": user.is_active
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to update user {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update user"
        )

@router.patch("/{user_id}/reset-password", dependencies=[Depends(require_roles("admin"))])
async def reset_password(
    user_id: str,
    payload: ResetPasswordRequest,
    db: AsyncSession = Depends(get_session)
):
    try:
        user = await db.get(User, user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        user.hashed_password = get_password_hash(payload.password)
        db.add(user)
        await db.commit()

        logger.info(f"Password reset for user: {user.email}")
        return {"message": "Password reset successfully"}
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to reset password for {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reset password"
        )

@router.delete("/{user_id}", dependencies=[Depends(require_roles("admin"))])
async def delete_user(
    user_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session)
):
    try:
        if str(current_user.id) == user_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You cannot deactivate your own account"
            )

        user = await db.get(User, user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        user.is_active = False
        db.add(user)
        await db.commit()

        logger.info(f"User deactivated: {user.email} by admin {current_user.email}")
        return {"message": "User deactivated successfully"}
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to deactivate user {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to deactivate user"
        )