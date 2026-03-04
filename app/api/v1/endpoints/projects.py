from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from pydantic import BaseModel, Field
from typing import Optional, List
from app.core.database import get_session
from app.models.project import Project
import logging
from app.schemas.project import ProjectCreate, ProjectResponse

logger = logging.getLogger("projects_router")
router = APIRouter()



@router.get("/", response_model=List[ProjectResponse])
async def list_projects(db: AsyncSession = Depends(get_session)):
    try:
        statement = select(Project).order_by(Project.created_at.desc())
        result = await db.execute(statement)
        return result.scalars().all()
    except Exception as e:
        logger.error(f"Failed to fetch projects: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch projects"
        )



@router.post("/", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(payload: ProjectCreate, db: AsyncSession = Depends(get_session)):
    try:
        existing = await db.execute(
            select(Project).where(Project.name == payload.name)
        )
        if existing.scalars().first():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Project '{payload.name}' already exists"
            )

        project = Project(**payload.model_dump())
        db.add(project)
        await db.commit()
        await db.refresh(project)
        return project

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Project creation failed: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create project"
        )

