from typing import Literal
import logging
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, func, case
from app.core.database import get_session
from app.models.product import Product
from app.models.project import Project
from app.schemas.dashboard import BrandAttributeStat, BrandFlowStat, CategoryAttributeStat, CategoryDistributionStat, CategoryFlowStat, DashboardMetricsResponse, TimelineStat
from typing import Optional, List
from datetime import datetime, date, time, timezone
logger = logging.getLogger("dashboard_metrics")
router = APIRouter()

DateField = Literal["created_at", "updated_at"]


def build_product_filters(
    project_id: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    date_field: DateField = "created_at",
):
    filters = []
    if project_id:
        filters.append(Product.project_id == project_id)
    col = Product.created_at if date_field == "created_at" else Product.updated_at
    if start_date:
        filters.append(col >= start_date)
    if end_date:
        filters.append(col <= end_date)
    return filters


async def calculate_metrics(
    db: AsyncSession,
    project_id: str | None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    date_field: DateField = "created_at",
) -> dict:
    filters = build_product_filters(project_id, start_date, end_date, date_field=date_field)

    stmt = select(
        func.count(Product.id).label("total"),
        func.sum(case((Product.enrichment_status == "completed", 1), else_=0)).label("aggregated"),
        func.sum(case((Product.enrichment_status == "failed", 1), else_=0)).label("failed"),
        func.sum(case((Product.enrichment_status == "pending", 1), else_=0)).label("pending"),
        func.avg(Product.completeness_score).label("health"),
    ).where(*filters)

    stats = (await db.execute(stmt)).first()

    # category distribution MUST also use the same filters (date + project)
    cat_expression = Product.taxonomy
    cat_stmt = (
        select(cat_expression, func.count(Product.id))
        .where(*filters)
        .group_by(cat_expression)
        .order_by(func.count(Product.id).desc())
        .limit(5)
    )
    cat_result = await db.execute(cat_stmt)
    categories = [{"category_name": row[0] or "Uncategorized", "count": row[1]} for row in cat_result.all()]

    # project stats unchanged
    total_projects = 0
    active_projects = 0
    project_name = "Global Overview"
    proj = None

    if project_id:
        proj = await db.get(Project, project_id)
        if proj:
            project_name = proj.name
            total_projects = 1
            active_projects = 1 if proj.status in ("processing", "partially_completed") else 0
    else:
        proj_stats = await db.execute(
    select(
        func.count(Project.id), 
        func.sum(case((Project.status.in_(["processing", "partially_completed"]), 1), else_=0))
    )
)
        p_row = proj_stats.first()
        total_projects = p_row[0] or 0
        active_projects = p_row[1] or 0

    completed_count = stats.aggregated or 0
    failed_count = stats.failed or 0
    pending_count = stats.pending or 0
    total_products = stats.total or 0

    aggregated_products = 0
    cleaned_products = 0
    enriched_products = 0
    published_products = 0

    # NOTE: these calculations currently do NOT respect date filters for enrichment routing logic.
    # If you want them to respect date range, apply *filters here too (optional).
    if project_id and proj:
        operation_mode = (proj.operation_mode or "").lower()
        use_case = (proj.use_case or "").lower()

        if operation_mode == "aggregation":
            aggregated_products = completed_count
        elif operation_mode == "cleaning":
            cleaned_products = completed_count
            if "standardization" in use_case or "validation" in use_case:
                cleaned_products = completed_count
        elif operation_mode == "enrichment":
            enriched_products = completed_count

        enrichment_stmt = select(func.count(Product.id)).where(*filters,((Product.workflow_stage == "enrichment") | (Product.needs_enrichment == True)),)
        enrichment_workflow_count = (await db.execute(enrichment_stmt)).scalar() or 0

        if operation_mode == "aggregation":
            enriched_products = enrichment_workflow_count
        elif operation_mode == "enrichment":
            enriched_products = max(enriched_products, enrichment_workflow_count)

    else:
        global_stmt = (select(Project.operation_mode, Project.use_case, func.count(Product.id)).join(Product, Product.project_id == Project.id).where(*filters,Product.enrichment_status == "completed",).group_by(Project.operation_mode, Project.use_case))
        global_result = await db.execute(global_stmt)

        for operation_mode, use_case, count in global_result.all():
            operation_mode = (operation_mode or "").lower()
            count = count or 0
            if operation_mode == "aggregation":
                aggregated_products += count
            elif operation_mode == "cleaning":
                cleaned_products += count
            elif operation_mode == "enrichment":
                enriched_products += count

        enrichment_workflow_stmt = select(func.count(Product.id)).where(*filters,(Product.workflow_stage == "enrichment") | (Product.needs_enrichment == True),)
        enrichment_workflow_count = (await db.execute(enrichment_workflow_stmt)).scalar() or 0
        enriched_products = max(enriched_products, enrichment_workflow_count)

    return {
        "name": project_name,
        "totalProjects": total_projects,
        "activeProjects": active_projects,
        "totalProducts": total_products,
        "aggregatedProducts": aggregated_products,
        "cleanedProducts": cleaned_products,
        "enrichedProducts": enriched_products,
        "publishedProducts": published_products,
        "failedProducts": failed_count,
        "pendingProducts": pending_count,
        "catalogHealth": int(stats.health or 0),
        "categoryDistribution": categories,
    }
@router.get("/metrics", response_model=DashboardMetricsResponse)
async def get_dashboard_metrics(
    start_date: Optional[str] = Query(None),end_date: Optional[str] = Query(None),date_field: DateField = Query("created_at"),db: AsyncSession = Depends(get_session),):    
    try:
        start_dt = parse_date(start_date, end=False)
        end_dt = parse_date(end_date, end=True)
        data = await calculate_metrics(db, None, start_dt, end_dt,date_field=date_field)
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
async def get_project_metrics(
    project_id: str,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    date_field: DateField = Query("created_at"),
    db: AsyncSession = Depends(get_session),
):
    try:
        logger.info(
            f"[DEBUG] get_project_metrics called with project_id: '{project_id}' (type: {type(project_id).__name__})")
        start_dt = parse_date(start_date,end=False)
        end_dt = parse_date(end_date,end=True)
        data = await calculate_metrics(db, project_id, start_dt, end_dt,date_field=date_field)
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


def parse_date(date_str: Optional[str], end: bool = False) -> Optional[datetime]:
    if not date_str:
        return None

    s = date_str.strip().replace("Z", "+00:00")

    # date-only: YYYY-MM-DD
    if len(s) == 10:
        d = date.fromisoformat(s)
        if end:
            return datetime.combine(d, time(23, 59, 59, 999999))  # naive
        return datetime.combine(d, time(0, 0, 0))  # naive

    dt = datetime.fromisoformat(s)
    # if aware, convert to naive UTC
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt

@router.get("/timeline", response_model=List[TimelineStat])
async def get_dashboard_timeline(
    project_id: Optional[str] = Query(None),
    period: str = Query("month"),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    date_field: DateField = Query("created_at"),
    db: AsyncSession = Depends(get_session)
):
    try:
        start_dt = parse_date(start_date,end=False)
        end_dt = parse_date(end_date,end=True)
        filters = build_product_filters(
            project_id, start_dt, end_dt, date_field=date_field)

        period = period.lower()
        if period not in {"day", "week", "month"}:
            period = "month"
        col = Product.created_at if date_field == "created_at" else Product.updated_at
        period_expr = func.date_trunc(period, col)

        stmt = select(
            period_expr.label("period"),
            func.count(Product.id).label("total_products"),
            func.sum(case((Product.enrichment_status == "completed", 1), else_=0)).label(
                "aggregated_products"),
            func.sum(case((
                (Product.workflow_stage == "enrichment") | (
                    Product.needs_enrichment == True), 1
            ), else_=0)).label("moved_to_enrichment")
        ).where(
            *filters
        ).group_by(
            period_expr
        ).order_by(
            period_expr
        )

        result = await db.execute(stmt)

        return [
            TimelineStat(
                period=row.period.isoformat() if row.period else "",
                totalProducts=row.total_products or 0,
                aggregatedProducts=row.aggregated_products or 0,
                movedToEnrichment=row.moved_to_enrichment or 0,
            )
            for row in result.all()
        ]
    except Exception as e:
        logger.error(f"Failed to load dashboard timeline: {e}", exc_info=True)
        return []


@router.get("/brand-flow", response_model=List[BrandFlowStat])
async def get_brand_flow(
    project_id: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    date_field: DateField = Query("created_at"),
    db: AsyncSession = Depends(get_session)
):
    try:
        start_dt = parse_date(start_date, end=False)
        end_dt = parse_date(end_date, end=True)
        filters = build_product_filters(
            project_id, start_dt, end_dt, date_field=date_field)

        stmt = select(
            Product.brand_name.label("brand"),
            func.count(Product.id).label("total_products"),
            func.sum(case((Product.enrichment_status == "completed", 1), else_=0)).label(
                "aggregated_products"),
            func.sum(case((
                (Product.workflow_stage == "enrichment") | (
                    Product.needs_enrichment == True), 1
            ), else_=0)).label("enrichment_products")
        ).where(
            *filters
        ).group_by(
            Product.brand_name
        ).order_by(
            func.count(Product.id).desc()
        )

        result = await db.execute(stmt)

        return [
            BrandFlowStat(
                brand=row.brand or "Unknown",
                totalProducts=row.total_products or 0,
                aggregatedProducts=row.aggregated_products or 0,
                enrichmentProducts=row.enrichment_products or 0,
            )
            for row in result.all()
        ]
    except Exception as e:
        logger.error(f"Failed to load brand flow metrics: {e}", exc_info=True)
        return []


@router.get("/brand-attributes", response_model=List[BrandAttributeStat])
async def get_brand_attributes(
    project_id: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    date_field: DateField = Query("created_at"),
    db: AsyncSession = Depends(get_session)
):
    try:
        start_dt = parse_date(start_date, end=False)
        end_dt = parse_date(end_date, end=True)
        filters = build_product_filters(
            project_id, start_dt, end_dt, date_field=date_field)

        stmt = select(
            Product.brand_name,
            Product.workflow_stage,
            Product.enrichment_status,
            Product.attributes,
            Product.dynamic_attributes
        ).where(*filters)

        result = await db.execute(stmt)
        rows = result.all()

        brand_map = {}

        for brand, workflow_stage, enrichment_status, attributes, dynamic_attributes in rows:
            brand_key = brand or "Unknown"

            if brand_key not in brand_map:
                brand_map[brand_key] = {
                    "brand": brand_key,
                    "aggregationAttributes": 0,
                    "enrichmentAttributes": 0,
                    "completedAttributes": 0,
                    "totalAttributes": 0
                }

            attr_count = 0
            if isinstance(attributes, dict):
                attr_count += len(attributes)
            if isinstance(dynamic_attributes, list):
                attr_count += len(dynamic_attributes)

            brand_map[brand_key]["totalAttributes"] += attr_count

            if workflow_stage == "aggregation":
                brand_map[brand_key]["aggregationAttributes"] += attr_count

            if workflow_stage == "enrichment":
                brand_map[brand_key]["enrichmentAttributes"] += attr_count

            if enrichment_status == "completed":
                brand_map[brand_key]["completedAttributes"] += attr_count

        return [BrandAttributeStat(**value) for value in brand_map.values()]

    except Exception as e:
        logger.error(
            f"Failed to load brand attribute metrics: {e}", exc_info=True)
        return []


@router.get("/category-attributes", response_model=List[CategoryAttributeStat])
async def get_category_attributes(
    project_id: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    date_field: DateField = Query("created_at"),
    db: AsyncSession = Depends(get_session)
):
    try:
        start_dt = parse_date(start_date, end=False)
        end_dt = parse_date(end_date, end=True)
        filters = build_product_filters(
            project_id, start_dt, end_dt, date_field=date_field)

        stmt = select(
            Product.taxonomy,
            Product.workflow_stage,
            Product.enrichment_status,
            Product.attributes,
            Product.dynamic_attributes
        ).where(*filters)

        result = await db.execute(stmt)
        rows = result.all()

        category_map = {}

        for taxonomy, workflow_stage, enrichment_status, attributes, dynamic_attributes in rows:
            cat_key = taxonomy or "Uncategorized"

            if cat_key not in category_map:
                category_map[cat_key] = {
                    "category": cat_key,
                    "aggregationAttributes": 0,
                    "enrichmentAttributes": 0,
                    "completedAttributes": 0,
                    "totalAttributes": 0
                }

            attr_count = 0
            if isinstance(attributes, dict):
                attr_count += len(attributes)
            if isinstance(dynamic_attributes, list):
                attr_count += len(dynamic_attributes)

            category_map[cat_key]["totalAttributes"] += attr_count

            if workflow_stage == "aggregation":
                category_map[cat_key]["aggregationAttributes"] += attr_count

            if workflow_stage == "enrichment":
                category_map[cat_key]["enrichmentAttributes"] += attr_count

            if enrichment_status == "completed":
                category_map[cat_key]["completedAttributes"] += attr_count

        return [CategoryAttributeStat(**value) for value in category_map.values()]

    except Exception as e:
        logger.error(
            f"Failed to load category attribute metrics: {e}", exc_info=True)
        return []


@router.get("/category-flow", response_model=List[CategoryFlowStat])
async def get_category_flow(
    project_id: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    date_field: DateField = Query("created_at"),
    db: AsyncSession = Depends(get_session)
):
    try:
        start_dt = parse_date(start_date, end=False)
        end_dt = parse_date(end_date, end=True)
        filters = build_product_filters(
            project_id, start_dt, end_dt, date_field=date_field)

        stmt = select(
            Product.taxonomy.label("category"),
            func.count(Product.id).label("total_products"),
            func.sum(case((Product.enrichment_status == "completed", 1), else_=0)).label(
                "aggregated_products"),
            func.sum(case((
                (Product.workflow_stage == "enrichment") | (
                    Product.needs_enrichment == True), 1
            ), else_=0)).label("enrichment_products")
        ).where(
            *filters
        ).group_by(
            Product.taxonomy
        ).order_by(
            func.count(Product.id).desc()
        )

        result = await db.execute(stmt)

        return [
            CategoryFlowStat(
                category=row.category or "Uncategorized",
                totalProducts=row.total_products or 0,
                aggregatedProducts=row.aggregated_products or 0,
                enrichmentProducts=row.enrichment_products or 0,
            )
            for row in result.all()
        ]
    except Exception as e:
        logger.error(
            f"Failed to load category flow metrics: {e}", exc_info=True)
        return []


@router.get("/category-distribution", response_model=List[CategoryDistributionStat])
async def get_category_distribution(
    project_id: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    date_field: DateField = Query("created_at"),
    db: AsyncSession = Depends(get_session)
):
    try:
        start_dt = parse_date(start_date, end=False)
        end_dt = parse_date(end_date, end=True)
        filters = build_product_filters(
            project_id, start_dt, end_dt, date_field=date_field)

        stmt = select(
            Product.taxonomy.label("category"),
            func.count(Product.id).label("product_count")
        ).where(
            *filters
        ).group_by(
            Product.taxonomy
        ).order_by(
            func.count(Product.id).desc()
        )

        result = await db.execute(stmt)
        rows = result.all()

        total_products = sum(row.product_count for row in rows)

        return [
            CategoryDistributionStat(
                category=row.category or "Uncategorized",
                productCount=row.product_count,
                percentage=round((row.product_count / total_products)
                                 * 100, 2) if total_products > 0 else 0.0
            )
            for row in rows
        ]
    except Exception as e:
        logger.error(
            f"Failed to load category distribution metrics: {e}", exc_info=True)
        return []
@router.get("/needs-attention")
async def get_needs_attention(
    project_id: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    date_field: DateField = Query("created_at"),
    db: AsyncSession = Depends(get_session)
):
    try:
        start_dt = parse_date(start_date, end=False)
        end_dt = parse_date(end_date, end=True)
        filters = build_product_filters(project_id, start_dt, end_dt, date_field=date_field)
        
        # Uncategorized products
        uncategorized_stmt = select(func.count(Product.id)).where(
            *filters,
            (Product.taxonomy == None) | (Product.taxonomy == "")
        )
        uncategorized = (await db.execute(uncategorized_stmt)).scalar() or 0
        
        # Invalid attributes (placeholder - you may have a CleansingIssue table)
        invalid_attributes = 0  # Replace with actual query
        
        # Pending aggregation
        pending_stmt = select(func.count(Product.id)).where(
            *filters,
            Product.enrichment_status == "pending"
        )
        pending = (await db.execute(pending_stmt)).scalar() or 0
        
        # Failed jobs
        failed_stmt = select(func.count(Product.id)).where(
            *filters,
            Product.enrichment_status == "failed"
        )
        failed = (await db.execute(failed_stmt)).scalar() or 0
        
        return {
            "uncategorized": uncategorized,
            "invalidAttributes": invalid_attributes,
            "pendingAggregation": pending,
            "failedJobs": failed
        }
    except Exception as e:
        logger.error(f"Failed to get needs attention: {e}", exc_info=True)
        return {"uncategorized": 0, "invalidAttributes": 0, "pendingAggregation": 0, "failedJobs": 0}

@router.get("/recent-activity")
async def get_recent_activity(
    project_id: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    date_field: DateField = Query("created_at"),
    limit: int = Query(10),
    db: AsyncSession = Depends(get_session)
):
    try:
        start_dt = parse_date(start_date, end=False)
        end_dt = parse_date(end_date, end=True)
        filters = build_product_filters(project_id, start_dt, end_dt, date_field=date_field)
        
        stmt = select(
            Product.id,
            Product.product_name,
            Product.product_code,
            Product.enrichment_status,
            Product.updated_at
        ).where(
            *filters
        ).order_by(
            Product.updated_at.desc()
        ).limit(limit)
        
        result = await db.execute(stmt)
        rows = result.all()
        
        activities = []
        for row in rows:
            activity_type = "completed" if row.enrichment_status == "completed" else row.enrichment_status
            activities.append({
                "type": activity_type,
                "title": row.product_name or row.product_code,
                "subtitle": f"Status: {row.enrichment_status}",
                "ts": row.updated_at.isoformat() if row.updated_at else ""
            })
        
        return activities
    except Exception as e:
        logger.error(f"Failed to get recent activity: {e}", exc_info=True)
        return []