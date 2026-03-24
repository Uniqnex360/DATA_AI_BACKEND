from sqlalchemy import cast, String

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, func, outerjoin, and_
from pydantic import BaseModel, Field
from typing import Optional, List
from app.core.database import get_session
from app.models.pipeline import AggregationJob
from app.models.product import Product
from app.models.project import Project
import logging
from sqlalchemy.orm import aliased

from app.schemas.project import ProjectCreate, ProjectResponse
logger = logging.getLogger("projects_router")
router = APIRouter()


@router.get("/", response_model=List[ProjectResponse])
async def list_projects(db: AsyncSession = Depends(get_session)):
    try:
        active_job = aliased(AggregationJob)
        statement = (
            select(
                Project,
                func.count(Product.id).label("product_count"),
                func.max(active_job.status).label("aggregation_status")
            )
            .outerjoin(Product, Product.project_id == Project.id)
            .outerjoin(
                active_job,
                and_(
                    active_job.project_id == cast(Project.id, String),
                    active_job.status.in_(["pending", "processing"])
                )
            )
            .group_by(Project.id)
            .order_by(Project.created_at.desc())
        )
        result = await db.execute(statement)
        rows = result.all()
        projects = []
        for row in rows:
            project = row[0]
            product_count = row[1]
            aggregation_status=row[2]
            project_dict = {
                "id": str(project.id),
                "name": project.name,
                "client": project.client,
                "use_case": project.use_case,
                "status": project.status,
                "created_at": project.created_at,
                "updated_at": project.updated_at,
                "product_count": product_count or 0,
                "aggregation_status": aggregation_status or "idle" 
            }
            projects.append(project_dict)
        return projects
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
