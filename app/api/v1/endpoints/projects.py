from sqlalchemy import cast, String,case, null, or_
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, func, outerjoin, and_
from typing import Optional, List
from app.core.database import get_session
from app.models.brand import Brand
from app.models.pipeline import AggregationJob, Source
from app.models.product import Product
from app.models.project import Project
import logging
from sqlalchemy.orm import aliased
from datetime import datetime, timedelta, timezone
from app.schemas.project import ProjectCreate, ProjectResponse
from app.utils.timezone import now_ist
logger = logging.getLogger("projects_router")

router = APIRouter()
def normalize_source_status(status: str | None, project_status: str | None = None) -> str:
    if project_status == "partially_completed":
        return "Partially Completed"
    if project_status == "processing" or project_status == "in_progress":
        return "In Progress"
    if project_status == "completed":
        return "Completed"
    if project_status == "failed":
        return "Failed"
    if project_status == "draft":
        return "Yet to Start"
    if not status:
        return "Yet to Start"
    status = status.lower().strip()
    if status == "completed":
        return "Completed"
    if status in ("processing", "in_progress", "in progress"):
        return "In Progress"
    if status == "failed":
        return "Failed"
    return "Yet to Start"

@router.get("/", response_model=List[ProjectResponse])
async def list_projects(
    operation_mode: str | None = None,
    tab: str | None = None,
    q: str | None = None,  
    db: AsyncSession = Depends(get_session),
):
    try:
        active_job = aliased(AggregationJob)
        active_source = aliased(Source)
        aggregated_count_subq = select(null()).label('aggregated_count')  
        
        if tab == "aggregation":
            product_count_subq = (
                select(func.count(Product.id))
                .where(
                    and_(
                        Product.project_id == Project.id,
                        Product.workflow_stage == "aggregation",
                        Product.enrichment_status.in_(["pending", "failed", "completed", "processing"])
                    )
                )
                .scalar_subquery()
                .label("product_count")
            )
            aggregated_count_subq = (
                select(func.count(Product.id))
                .where(Product.project_id == Project.id, Product.workflow_stage == 'aggregation', Product.enrichment_status == 'completed')
                .scalar_subquery()
                .label('aggregated_count')
            )
        elif tab == "enrichment":
            product_count_subq = (
                select(func.count(Product.id))
                .where(
                    and_(
                        Product.project_id == Project.id,
                        Product.workflow_stage == "enrichment",
                        Product.enrichment_status.in_(["pending", "failed"])
                    )
                )
                .scalar_subquery()
                .label("product_count")
            )
        else:
            product_count_subq = (
                select(func.count(Product.id))
                .where(Product.project_id == Project.id)
                .scalar_subquery()
                .label("product_count")
            )
        cleaned_count_subq = (
            select(func.count(Product.id))
            .where(and_(Product.project_id == Project.id, Product.enrichment_status == 'completed'))
            .scalar_subquery().label('cleaned_count')
        )
        failed_count_subq = (
            select(func.count(Product.id))
            .where(and_(Product.project_id == Project.id, Product.enrichment_status == 'failed'))
            .scalar_subquery().label('failed_count')
        )
        pending_count_subq = (
            select(func.count(Product.id))
            .where(and_(Product.project_id == Project.id, Product.enrichment_status == 'pending'))
            .scalar_subquery().label('pending_count')
        )
        completeness_subq = (
            select(func.avg(Product.completeness_score))
            .where(and_(Product.project_id == Project.id, Product.workflow_stage == "aggregation"))
            .scalar_subquery()
            .label("completeness_score")
        )
        data_quality_subq = (
    select(func.avg(Product.data_quality_score))
    .where(Product.project_id == Project.id)
    .scalar_subquery()
    .label("data_quality_score")
)
        
        statement = (
            select(
                Project,
                product_count_subq,
                cleaned_count_subq,
                failed_count_subq,
                pending_count_subq,
                aggregated_count_subq,
                completeness_subq,
                data_quality_subq,
                func.max(active_job.status).label("processing_status"),
                func.max(cast(active_source.source_metadata["processing_status"], String)).label("source_status"),
            )
            .outerjoin(active_job, and_(active_job.project_id == cast(Project.id, String), active_job.status.in_(["pending", "processing", "completed", "failed"])))
            .outerjoin(active_source, active_source.project_id == Project.id)
        )
        if q:
            search_term = f"%{q}%"
            statement = statement.where(
                or_(
                    Project.name.ilike(search_term),
                    Project.description.ilike(search_term)
                )
            )
        
        if operation_mode:
            if ',' in operation_mode:
                modes = operation_mode.split(',')
                statement = statement.where(Project.operation_mode.in_(modes))
            else:
                statement = statement.where(Project.operation_mode == operation_mode)
        
        statement = statement.group_by(Project.id).order_by(Project.created_at.desc())
        result = await db.execute(statement)
        rows = result.all()
        projects = []
        
        for row in rows:
            project = row[0]
            product_count = row[1] or 0
            cleaned_count = row[2] or 0
            failed_count = row[3] or 0
            pending_count = row[4] or 0
            aggregated_count = row[5] or 0
            completeness_score = row[6] or 0
            data_quality_score=row[7] or 0
            processing_status = row[8]
            source_status = row[9]
            
            clean_source_status = source_status.replace('"', "") if source_status else None
            
            project_response = ProjectResponse.model_validate(project)
            project_response.product_count = product_count
            project_response.cleaned_count = cleaned_count
            project_response.failed_count = failed_count
            project_response.pending_count = pending_count
            project_response.aggregated_count = aggregated_count
            project_response.completeness_score = round(completeness_score, 1) if completeness_score else 0
            project_response.data_quality_score=round(data_quality_score,1) if data_quality_score else 0
            project_response.processing_status = processing_status or "pending"
            project_response.source_status = normalize_source_status(clean_source_status, project_status=project.status)
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
        project_data = payload.model_dump()
        project_data['created_at'] = now_ist()
        project_data['updated_at'] = now_ist()
        project = Project(**project_data)
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