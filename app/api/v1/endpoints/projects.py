from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from app.core.database import get_session
from app.models.project import Project
import logging

logger = logging.getLogger("projects_router")
router = APIRouter()

@router.get("/")
async def list_projects(db: AsyncSession = Depends(get_session)):
    try:
        statement = select(Project).order_by(Project.created_at.desc())
        result = await db.execute(statement)
        return result.scalars().all()
    except Exception as e:
        logger.error(f"Failed to fetch projects: {e}")
        return []

@router.post("/")
async def create_project(project_data: Project, db: AsyncSession = Depends(get_session)):
    try:
        db.add(project_data)
        await db.commit()
        await db.refresh(project_data)
        return project_data
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to create project")