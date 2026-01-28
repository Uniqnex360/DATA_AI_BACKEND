from sqlalchemy.ext.asyncio import AsyncSession
from app.models.pipeline import CleansingIssue
from fastapi import HTTPException
from sqlmodel import select
import logging
logger=logging.getLogger('cleaning_service')

class CleaningService:
    async def get_all_issues(self,db:AsyncSession):
        try:
            statement=select(CleansingIssue).order_by(CleansingIssue.detected_at)
            result=await db.execute(statement)
            return result.all()
        except Exception as e:
            logger.error(f"Failed to fetch cleaning issues :{str(e)}")
            raise HTTPException(status_code=500, detail="Error retrieving data quality issues") 
    async def resolve_issue(self,db:AsyncSession,issue_id:str):
        try:
            issue=await db.get(CleansingIssue,issue_id)
            if not issue:
                logger.warning(f"Cleaning issue not found ${issue_id}")
                return None
            issue.resolved=True
            db.add(issue)
            await db.commit()
            await db.refresh(issue)
            return issue
        except Exception as e:
            await db.rollback()
            logger.error(f"Failed to resolve cleansing issue {issue_id}:{str(e)}")
            raise HTTPException(status_code=500,detail="Database error while resolving issues")
cleaning_service=CleaningService()
            