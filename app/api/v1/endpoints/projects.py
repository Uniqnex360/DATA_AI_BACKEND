from sqlalchemy import cast, String

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, func, outerjoin, and_
from pydantic import BaseModel, Field
from typing import Optional, List
from app.core.database import get_session
from app.models.brand import Brand
from app.models.pipeline import AggregationJob, Source
from app.models.product import Product
from app.models.project import Project
import logging
from sqlalchemy.orm import aliased

from app.schemas.project import ProjectCreate, ProjectResponse
logger = logging.getLogger("projects_router")
router = APIRouter()
def normalize_source_status(status: str | None) -> str:
    if not status:
        return "Yet to Start"
    
    status = status.lower().strip()
    
    if status == "completed":
        return "Completed"
    if status in ("processing", "failed"):
        return "In Progress"
    
    return "Yet to Start"
@router.get("/", response_model=List[ProjectResponse])
async def list_projects(
    operation_mode: str | None = None,
    db: AsyncSession = Depends(get_session),
):
    try:
        active_job = aliased(AggregationJob)
        active_source = aliased(Source)

        statement = (
            select(
                Project,
                func.count(Product.id).label("product_count"),
                func.max(active_job.status).label("processing_status"),
                func.max(
                    cast(active_source.source_metadata["processing_status"], String)
                ).label("source_status"),
            )
            .outerjoin(Product, Product.project_id == Project.id)
            .outerjoin(
                active_job,
                and_(
                    active_job.project_id == cast(Project.id, String),
                    active_job.status.in_(["pending", "processing", "completed", "failed"]),
                ),
            )
            .outerjoin(
                active_source,
                active_source.project_id == Project.id,
            )
        )

        if operation_mode:
            statement = statement.where(Project.operation_mode == operation_mode)

        statement = (
            statement.group_by(Project.id)
            .order_by(Project.created_at.desc())
        )

        result = await db.execute(statement)
        rows = result.all()

        projects = []
        for row in rows:
            project = row[0]
            product_count = row[1]
            processing_status = row[2]
            source_status = row[3]
            clean_source_status = source_status.replace('"', "") if source_status else None

            project_response = ProjectResponse.model_validate(project)
            project_response.product_count = product_count or 0
            project_response.processing_status = processing_status or "pending"
            project_response.source_status = normalize_source_status(clean_source_status)


            projects.append(project_response)

        return projects
    except Exception as e:
        logger.error(f"Failed to fetch projects: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch projects",
        )

@router.post("/", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(payload: ProjectCreate, db: AsyncSession = Depends(get_session)):
    print(f"Received payload: {payload}")
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
@router.get("/filters")
async def get_project_filters(
    project_id: str | None = None,
    db: AsyncSession = Depends(get_session),
):
    try:
        category_stmt = select(Product.category_1)
        brand_stmt = select(Brand.name).join(Product, Product.brand_id == Brand.id)

        if project_id:
            category_stmt = category_stmt.where(Product.project_id == project_id)
            brand_stmt = brand_stmt.where(Product.project_id == project_id)

        category_result = await db.execute(category_stmt)
        category_rows = category_result.all()

        categories = sorted(
            {
                row[0].strip()
                for row in category_rows
                if row[0] and isinstance(row[0], str) and row[0].strip()
            }
        )

        brand_result = await db.execute(brand_stmt)
        brand_rows = brand_result.all()

        brands = sorted(
            {
                row[0].strip()
                for row in brand_rows
                if row[0] and isinstance(row[0], str) and row[0].strip()
            }
        )

        return {
            "categories": categories,
            "brands": brands,
        }

    except Exception as e:
        logger.error(
            f"Failed to fetch filters for project {project_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch project filters",
        )