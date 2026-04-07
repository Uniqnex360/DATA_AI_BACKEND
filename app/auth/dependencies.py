from alembic.command import current
from curl_cffi import AsyncSession
from fastapi.security import OAuth2PasswordBearer
from fastapi import Depends, HTTPException, status
from app.core.config import settings
from app.core.database import get_session
from app.models.user import User
from typing import Optional
from jose import JWTError, jwt
SECRET_KEY=settings.SECRET_KEY
ALGORITHM=settings.ALGORITHM
oauth2_scheme=OAuth2PasswordBearer(tokenUrl='/api/v1/auth/login')
async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_session),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
    )
    try:
        payload=jwt.decode(token,SECRET_KEY,algorithms=[ALGORITHM])
        user_id:Optional[str]=payload.get('sub')
        if user_id is None:
            raise credentials_exception
        
    except JWTError:
        raise credentials_exception
    user=await db.get(User,user_id)
    if not user:
        raise credentials_exception
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,detail='Inactive User')
    return user
def require_roles(*allowed_roles:str):
    async def role_checker(current_user:User=Depends(get_current_user),)->User:
        if current_user.role not in allowed_roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,detail='You dont have permission to access this resource')
        return current_user
    return role_checker