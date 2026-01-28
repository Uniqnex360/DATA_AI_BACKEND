from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from app.core.database import get_session
from app.models.pipeline import CleansingIssue
import logging

logger = logging.getLogger("cleansing_router")
router = APIRouter()

@router.get("/issues")
async def get_all_issues(db: AsyncSession = Depends(get_session)):
    try:
        statement = select(CleansingIssue).order_by(CleansingIssue.detected_at.desc())
        result = await db.execute(statement)
        return result.scalars().all()
    except Exception as e:
        logger.error(f"Failed to fetch cleansing issues: {e}")
        return []

@router.post("/resolve/{issue_id}")
async def resolve_issue(issue_id: str, db: AsyncSession = Depends(get_session)):
    try:
        issue = await db.get(CleansingIssue, issue_id)
        if not issue:
            raise HTTPException(status_code=404, detail="Issue not found")
        
        issue.resolved = True
        db.add(issue)
        await db.commit()
        return {"status": "success"}
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))