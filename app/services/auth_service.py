from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from app.models.user import User
from app.core.security import verify_password

async def authenticate_user(db: AsyncSession, email: str, password: str) -> Optional[User]:
    statement = select(User).where(User.email == email)
    result = await db.exec(statement)
    user = result.first()
    
    if not user:
        return None
        
    if not verify_password(password, user.hashed_password):
        return None
        
    return user