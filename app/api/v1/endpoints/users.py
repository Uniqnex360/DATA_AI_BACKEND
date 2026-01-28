from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_session
from sqlmodel import select
from app.models.user import User
import logging

logger = logging.getLogger("user_router")
router = APIRouter()

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