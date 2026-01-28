from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_session
from sqlmodel import select
from app.models.pipeline import AuditTrail
import logging

logger = logging.getLogger("audit_router")
router = APIRouter()

@router.get("/")
async def get_audit_trail(db: AsyncSession = Depends(get_session)):
    try:
        statement = select(AuditTrail).order_by(AuditTrail.logged_at.desc()).limit(100)
        result = await db.execute(statement)
        logs = result.scalars().all()
        return logs
    except Exception as e:
        logger.error(f"Audit Trail Error: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fetch the audit trail")