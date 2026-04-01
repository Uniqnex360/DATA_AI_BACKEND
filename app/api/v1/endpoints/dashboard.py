import logging
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, func, case
from app.core.database import get_session
from app.models.product import Product
from app.models.project import Project
from app.schemas.dashboard import DashboardMetricsResponse
from typing import Dict, Any
logger = logging.getLogger("dashboard_metrics")
router = APIRouter()


async def calculate_metrics(db: AsyncSession, project_id: str | None) -> dict:
    try:
        if project_id:
            logger.info(
                f"[DEBUG] calculate_metrics for project_id: '{project_id}'")
        product_filters = []
        if project_id:
            product_filters.append(Product.project_id == project_id)
        stmt = select(
            func.count(Product.id).label("total"),
            func.sum(case((Product.enrichment_status == 'completed', 1), else_=0)).label(
                "aggregated"),
            func.sum(case((Product.enrichment_status == 'failed', 1), else_=0)).label(
                "failed"),
            func.sum(case((Product.enrichment_status == 'pending', 1), else_=0)).label(
                "pending"),
            func.avg(Product.completeness_score).label("health")
        )
        if project_id:
            stmt = stmt.where(Product.project_id == project_id)
            logger.info(
                f"[DEBUG] Query filter: Product.project_id == '{project_id}'")
        result = await db.execute(stmt)
        stats = result.first()
        logger.info(
            f"[DEBUG] Query result stats: total={stats.total}, aggregated={stats.aggregated}")
        # cat_expression = func.json_extract_path_text(
        #     Product.attributes, 'taxonomy')
        cat_expression=Product.taxonomy
        cat_stmt = (select(cat_expression, func.count(Product.id)).where(
            *product_filters).group_by(cat_expression).order_by(func.count(Product.id).desc()).limit(5))
        cat_result = await db.execute(cat_stmt)
        categories = [{"category_name": row[0] or "Uncategorized",
                       "count": row[1]} for row in cat_result.all()]
        total_projects = 0
        active_projects = 0
        project_name = 'Global Overview'
        if project_id:
            proj = await db.get(Project, project_id)
            if proj:
                project_name = proj.name
                total_projects = 1
                active_projects = 1 if proj.status == 'active' else 0
        else:
            proj_stats = await db.execute(
                select(func.count(Project.id), func.sum(case((Project.status == 'active', 1), else_=0))))
            p_row = proj_stats.first()
            total_projects = p_row[0] or 0
            active_projects = p_row[1] or 0
        completed_count = stats.aggregated or 0
        failed_count = stats.failed or 0
        pending_count = stats.pending or 0
        total_products = stats.total or 0

        aggregated_products = 0
        cleaned_products = 0
        standardized_products = 0
        enriched_products = 0
        published_products = 0

        if project_id and proj:
            operation_mode = (proj.operation_mode or "").lower()
            use_case = (proj.use_case or "").lower()

            if operation_mode == "aggregation":
                aggregated_products = completed_count
                

            elif operation_mode == "cleaning":
                cleaned_products = completed_count

                if "standardization" in use_case:
                    standardized_products = completed_count

            elif operation_mode == "enrichment":
                enriched_products = completed_count

                if "validation" in use_case:
                    standardized_products = completed_count

        else:
            global_stmt = select(
                Project.operation_mode,
                Project.use_case,
                func.count(Product.id)
            ).join(
                Product, Product.project_id == Project.id
            ).where(
                Product.enrichment_status == 'completed'
            ).group_by(
                Project.operation_mode,
                Project.use_case
            )

            global_result = await db.execute(global_stmt)

            for operation_mode, use_case, count in global_result.all():
                operation_mode = (operation_mode or "").lower()
                use_case = (use_case or "").lower()
                count = count or 0

                if operation_mode == "aggregation":
                    aggregated_products += count
                    

                elif operation_mode == "cleaning":
                    cleaned_products += count

                    if "standardization" in use_case:
                        standardized_products += count

                elif operation_mode == "enrichment":
                    enriched_products += count

                    if "validation" in use_case:
                        standardized_products += count

        return {
            "name": project_name,
            "totalProjects": total_projects,
            "activeProjects": active_projects,
            "totalProducts": total_products,
            "aggregatedProducts": aggregated_products,
            "cleanedProducts": cleaned_products,
            "standardizedProducts": standardized_products,
            "enrichedProducts": enriched_products,
            "publishedProducts": published_products,
            "failedProducts": failed_count,
            "pendingProducts": pending_count,
            "catalogHealth": int(stats.health or 0),
            "categoryDistribution": categories
        }
    except Exception as e:
        raise e


@router.get("/metrics", response_model=DashboardMetricsResponse)
async def get_dashboard_metrics(db: AsyncSession = Depends(get_session)):
    try:
        data = await calculate_metrics(db, None)
        logger.info(
            f"Global dashboard metrics loaded :{data['totalProducts']} products")
        return data
    except Exception as e:
        logger.error(
            f"CRITICAL: Failed to calculate dashboard metrics: {str(e)}")
        return {
            "totalProjects": 0, "activeProjects": 0, "totalProducts": 0,
            "publishedProducts": 0, "catalogHealth": 0, "categoryDistribution": []
        }


@router.get('/metrics/{project_id}', response_model=DashboardMetricsResponse)
async def get_project_metrics(project_id: str, db: AsyncSession = Depends(get_session)):
    try:
        logger.info(
            f"[DEBUG] get_project_metrics called with project_id: '{project_id}' (type: {type(project_id).__name__})")
        data = await calculate_metrics(db, project_id)
        logger.info(
            f"[DEBUG] Project metrics loaded for {project_id}: totalProducts={data.get('totalProducts', 0)}")
        return data
    except Exception as e:
        logger.error(
            f"Failed to fetch project metrics for {project_id}:{str(e)}", exc_info=True)
        return {
            "totalProjects": 0, "activeProjects": 0, "totalProducts": 0,
            "publishedProducts": 0, "catalogHealth": 0, "categoryDistribution": []
        }
