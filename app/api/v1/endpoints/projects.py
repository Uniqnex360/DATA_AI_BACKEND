from sqlalchemy import cast, String, case, null, or_
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, func, outerjoin, and_
from typing import Optional, List
from app.auth.dependencies import get_current_user
from app.auth.rbac import get_auth_filters
from app.core.database import get_session
from app.models.brand import Brand
from app.models.pipeline import AggregationJob, Source
from app.models.product import Product
from app.models.project import Project
import logging
from sqlalchemy.orm import aliased
from datetime import datetime, timedelta, timezone
from app.models.project_product_link import ProjectProductLink
from app.models.user import User
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
    current_user: User = Depends(get_current_user),
    user_id: Optional[str] = Query(None)
):
    try:
        active_job = aliased(AggregationJob)
        active_source = aliased(Source)
        aggregated_count_subq = select(null()).label('aggregated_count')
        auth_filter = get_auth_filters(current_user, user_id)
        if tab == "aggregation":
            product_count_subq = (
                select(func.count(Product.id))
                # ✅ ADD
                .join(ProjectProductLink, Product.id == ProjectProductLink.product_id)
                .join(Project, Project.id == ProjectProductLink.project_id)  # ✅ ADD
                .where(
                    and_(
                        ProjectProductLink.project_id == Project.id,  # ✅ CHANGE
                        Product.workflow_stage == "aggregation",
                        Product.enrichment_status.in_(
                            ["pending", "failed", "completed", "processing"])
                    )
                )
                .scalar_subquery()
                .label("product_count")
            )
            aggregated_count_subq = (
                select(func.count(Product.id))
                # ✅ ADD
                .join(ProjectProductLink, Product.id == ProjectProductLink.product_id)
                .join(Project, Project.id == ProjectProductLink.project_id)  # ✅ ADD
                .where(
                    ProjectProductLink.project_id == Project.id,  # ✅ CHANGE
                    Product.workflow_stage == 'aggregation',
                    Product.enrichment_status == 'completed'
                )
                .scalar_subquery()
                .label('aggregated_count')
            )
        elif tab == "enrichment":
            product_count_subq = (
                select(func.count(Product.id))
                # ✅ ADD
                .join(ProjectProductLink, Product.id == ProjectProductLink.product_id)
                .join(Project, Project.id == ProjectProductLink.project_id)  # ✅ ADD
                .where(
                    and_(
                        ProjectProductLink.project_id == Project.id,  # ✅ CHANGE
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
                # ✅ ADD
                .join(ProjectProductLink, Product.id == ProjectProductLink.product_id)
                .join(Project, Project.id == ProjectProductLink.project_id)  # ✅ ADD
                .where(ProjectProductLink.project_id == Project.id)  # ✅ CHANGE
                .scalar_subquery()
                .label("product_count")
            )
        cleaned_count_subq = (
            select(func.count(Product.id))
            # ✅ ADD
            .join(ProjectProductLink, Product.id == ProjectProductLink.product_id)
            .join(Project, Project.id == ProjectProductLink.project_id)  # ✅ ADD
            .where(
                and_(
                    ProjectProductLink.project_id == Project.id,  # ✅ CHANGE
                    Product.enrichment_status == 'completed'
                )
            )
            .scalar_subquery()
            .label('cleaned_count')
        )
        failed_count_subq = (
            select(func.count(Product.id))
            # ✅ ADD
            .join(ProjectProductLink, Product.id == ProjectProductLink.product_id)
            .join(Project, Project.id == ProjectProductLink.project_id)  # ✅ ADD
            .where(
                and_(
                    ProjectProductLink.project_id == Project.id,  # ✅ CHANGE
                    Product.enrichment_status == 'failed'
                )
            )
            .scalar_subquery()
            .label('failed_count')
        )
        pending_count_subq = (
            select(func.count(Product.id))
            # ✅ ADD
            .join(ProjectProductLink, Product.id == ProjectProductLink.product_id)
            .join(Project, Project.id == ProjectProductLink.project_id)  # ✅ ADD
            .where(
                and_(
                    ProjectProductLink.project_id == Project.id,  # ✅ CHANGE
                    Product.enrichment_status == 'pending'
                )
            )
            .scalar_subquery()
            .label('pending_count')
        )
        completeness_subq = (
            select(func.avg(Product.completeness_score))
            # ✅ ADD
            .join(ProjectProductLink, Product.id == ProjectProductLink.product_id)
            .join(Project, Project.id == ProjectProductLink.project_id)  # ✅ ADD
            .where(
                and_(
                    ProjectProductLink.project_id == Project.id,  # ✅ CHANGE
                    Product.workflow_stage == "aggregation"
                )
            )
            .scalar_subquery()
            .label("completeness_score")
        )
        data_quality_subq = (
            select(func.avg(Product.data_quality_score))
            # ✅ ADD
            .join(ProjectProductLink, Product.id == ProjectProductLink.product_id)
            .join(Project, Project.id == ProjectProductLink.project_id)  # ✅ ADD
            .where(ProjectProductLink.project_id == Project.id)  # ✅ CHANGE
            .scalar_subquery()
            .label("data_quality_score")
        )
        enrichment_pending_count_subq = (
            select(func.count(Product.id))
            # ✅ ADD
            .join(ProjectProductLink, Product.id == ProjectProductLink.product_id)
            .join(Project, Project.id == ProjectProductLink.project_id)  # ✅ ADD
            .where(
                and_(
                    ProjectProductLink.project_id == Project.id,  # ✅ CHANGE
                    Product.workflow_stage == 'enrichment',
                    Product.enrichment_status == 'pending'
                )
            )
            .scalar_subquery()
            .label('enrichment_pending_count')
        )

        algorithm_used_subq = (
            select(
                case(
                    (and_(
                        func.json_extract_path_text(
                            AggregationJob.details, 'llm_provider') == 'openai',
                        func.json_extract_path_text(
                            AggregationJob.details, 'missing_llm_provider') == 'gemini'
                    ), 'Algo 1 & 2'),
                    (and_(
                        func.json_extract_path_text(
                            AggregationJob.details, 'llm_provider') == 'gemini',
                        func.json_extract_path_text(
                            AggregationJob.details, 'missing_llm_provider') == 'openai'
                    ), 'Algo 2 & 1'),
                    (func.json_extract_path_text(AggregationJob.details,
                     'llm_provider') == 'openai', 'Datavio Algo-1'),
                    (func.json_extract_path_text(AggregationJob.details,
                     'llm_provider') == 'gemini', 'Datavio Algo-2'),
                    (func.json_extract_path_text(AggregationJob.details,
                     'llm_provider') == 'claude', 'Datavio Algo-3'),
                    else_=None
                )
            )
            .where(
                AggregationJob.project_id == cast(Project.id, String),
                AggregationJob.status.in_(['completed', 'processing'])
            )
            # ← Make sure this is the latest
            .order_by(AggregationJob.created_at.desc())
            .limit(1)
            .correlate(Project)
            .scalar_subquery()
            .label("algorithm_used")
        )
        source_subq = (
            select(func.coalesce(
                func.max(case(
                    (Source.source_type == "excel", Source.source_url),
                    else_=None
                )),
                func.max(Source.source_url)
            ))
            .where(Source.project_id == Project.id)
            .scalar_subquery()
            .label("import_file_name")
        )

        source_status_subq = (
            select(func.max(
                cast(Source.source_metadata["processing_status"], String)
            ))
            .where(Source.project_id == Project.id)
            .scalar_subquery()
            .label("source_processing_status")
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
                enrichment_pending_count_subq,
                source_subq,
                algorithm_used_subq,
                source_status_subq,
                func.max(active_job.status).label("processing_status"),
                func.max(cast(active_source.source_metadata["processing_status"], String)).label(
                    "source_status"),
            )
            .outerjoin(active_job, and_(active_job.project_id == cast(Project.id, String), active_job.status.in_(["pending", "processing", "completed", "failed"])))
            .outerjoin(active_source, active_source.project_id == Project.id)
        )
        # if current_user.role != "admin":
        #     statement = statement.where(Project.owner_id == current_user.id)
        if auth_filter:
            statement = statement.where(*auth_filter)
        if q:
            search_term = f"%{q}%"
            statement = statement.where(
                Project.name.ilike(search_term)
            )

        if operation_mode:
            if ',' in operation_mode:
                modes = operation_mode.split(',')
                statement = statement.where(Project.operation_mode.in_(modes))
            else:
                statement = statement.where(
                    Project.operation_mode == operation_mode)

        statement = statement.group_by(
            Project.id).order_by(Project.created_at.desc())
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
            data_quality_score = row[7] or 0
            enrichment_pending_count = row[8] or 0
            import_file_name = row[9]
            algorithm_used = row[10]
            source_processing_status = row[11]
            processing_status = row[12]
            source_status = row[13]

            clean_source_status = source_status.replace(
                '"', "") if source_status else None

            project_response = ProjectResponse.model_validate(project)
            project_response.product_count = product_count
            project_response.cleaned_count = cleaned_count
            project_response.failed_count = failed_count
            project_response.pending_count = pending_count
            project_response.aggregated_count = aggregated_count
            project_response.completeness_score = round(
                completeness_score, 1) if completeness_score else 0
            project_response.data_quality_score = round(
                data_quality_score, 1) if data_quality_score else 0
            project_response.enrichment_pending_count = enrichment_pending_count
            project_response.import_file_name = import_file_name
            project_response.algorithm_used = algorithm_used
            project_response.source_processing_status = source_processing_status.replace(
                '"', '') if source_processing_status else None
            project_response.processing_status = processing_status or "pending"
            project_response.source_status = normalize_source_status(
                clean_source_status, project_status=project.status)
            projects.append(project_response)

        return projects
    except Exception as e:
        logger.error(f"Failed to fetch projects: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch projects",
        )


@router.post("/", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(payload: ProjectCreate, db: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)):
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
        project_data['owner_id'] = current_user.id
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
    brand: str | None = None,
    category: str | None = None,
    workflow_stage: str | None = None,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user)
):

    try:
        category_stmt = select(
            func.coalesce(
                Product.category_8,
                Product.category_7,
                Product.category_6,
                Product.category_5,
                Product.category_4,
                Product.category_3,
                Product.category_2,
                Product.category_1
            )
        ).join(
            ProjectProductLink, Product.id == ProjectProductLink.product_id  # ✅ ADD
        ).join(
            Project, Project.id == ProjectProductLink.project_id  # ✅ ADD
        )
        brand_stmt = select(Brand.name).join(
            Product, Product.brand_id == Brand.id
        ).join(
            ProjectProductLink, Product.id == ProjectProductLink.product_id  # ✅ ADD
        ).join(
            Project, Project.id == ProjectProductLink.project_id  # ✅ ADD
        )
        # if current_user.role != "admin":
        #     category_stmt = category_stmt.join(Project, ProjectProductLink.project_id,  == Project.id).where(
        #         Project.owner_id == current_user.id)
        #     brand_stmt = brand_stmt.join(Project, ProjectProductLink.project_id,  == Project.id).where(
        #         Project.owner_id == current_user.id)
        if current_user.role != "admin":
            category_stmt = category_stmt.where(
                Project.owner_id == current_user.id)
            brand_stmt = brand_stmt.where(Project.owner_id == current_user.id)
        if project_id:
            category_stmt = category_stmt.where(
                ProjectProductLink.project_id == project_id)  # ✅ Already correct
            brand_stmt = brand_stmt.where(
                ProjectProductLink.project_id == project_id)
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
