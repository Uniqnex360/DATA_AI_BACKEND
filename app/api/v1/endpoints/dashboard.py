from typing import Literal
import logging
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, func, case
from app.core.database import get_session
from app.models.product import Product
from app.models.project import Project
from app.schemas.dashboard import BrandAttributeStat, BrandFlowStat, CategoryAttributeStat, CategoryDistributionStat, CategoryFlowStat, DashboardMetricsResponse, ProjectOverview, TimelineStat
from typing import Optional, List
from datetime import datetime, date, time, timezone
from app.models.product_attribute_link import ProductAttributeLinkModel
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
    mode: Optional[str] = None
) -> dict:
    try:
        filters = build_product_filters(
            project_id,
            start_date,
            end_date,
            date_field=date_field
        )
        stmt = select(
            func.count(Product.id).label("total"),
            func.sum(case((Product.enrichment_status == "completed", 1), else_=0)).label(
                "aggregated"),
            func.sum(case((Product.enrichment_status == "failed", 1), else_=0)).label(
                "failed"),
            func.sum(case((Product.enrichment_status == "pending", 1), else_=0)).label(
                "pending"),
            func.avg(Product.completeness_score).label("health"),
        )
        if mode and mode in ["aggregation", "cleaning", "enrichment"]:
            stmt = stmt.join(Project, Product.project_id == Project.id).where(
                Project.operation_mode == mode, *filters)
        else:
            stmt = stmt.where(*filters)
        brand_count_stmt = select(func.count(
            func.distinct(Product.brand_name)))
        if mode and mode in ["aggregation", "cleaning", "enrichment"]:
            brand_count_stmt = brand_count_stmt.join(Project, Product.project_id == Project.id).where(
                Project.operation_mode == mode, Product.brand_name.isnot(
                    None), Product.brand_name != "", *filters
            )
        else:
            brand_count_stmt = brand_count_stmt.where(
                Product.brand_name.isnot(None), Product.brand_name != "", *filters)
        cat_count_stmt = select(func.count(func.distinct(Product.taxonomy)))
        if mode and mode in ["aggregation", "cleaning", "enrichment"]:
            cat_count_stmt = cat_count_stmt.join(Project, Product.project_id == Project.id).where(
                Project.operation_mode == mode, Product.taxonomy.isnot(
                    None), Product.taxonomy != "", *filters
            )
        else:
            cat_count_stmt = cat_count_stmt.where(
                Product.taxonomy.isnot(None), Product.taxonomy != "", *filters)
        brand_count_stmt = select(func.count(func.distinct(Product.brand_name))).where(
            Product.brand_name.isnot(None), Product.brand_name != "", *filters
        )
        total_brands = (await db.execute(brand_count_stmt)).scalar() or 0
        cat_count_stmt = select(func.count(func.distinct(Product.taxonomy))).where(
            Product.taxonomy.isnot(None), Product.taxonomy != "", *filters
        )
        total_categories = (await db.execute(cat_count_stmt)).scalar() or 0
        stats = (await db.execute(stmt)).first()
        completed_count = stats.aggregated or 0
        failed_count = stats.failed or 0
        pending_count = stats.pending or 0
        total_products = stats.total or 0
        cat_expression = Product.taxonomy
        cat_stmt = (
            select(cat_expression, func.count(Product.id))
            .where(*filters)
            .group_by(cat_expression)
            .order_by(func.count(Product.id).desc())
            .limit(5)
        )
        cat_result = await db.execute(cat_stmt)
        categories = [
            {
                "category_name": row[0] or "Uncategorized",
                "count": row[1]
            }
            for row in cat_result.all()
        ]
        total_projects = 0
        active_projects = 0
        project_name = "Global Overview"
        proj = None
        if project_id:
            proj = await db.get(Project, project_id)
            if proj:
                project_name = proj.name
                total_projects = 1
                active_projects = (
                    1 if proj.status in (
                        "processing", "partially_completed") else 0
                )
        else:
            proj_stats = await db.execute(
                select(
                    func.count(Project.id),
                    func.sum(
                        case(
                            (
                                Project.status.in_(
                                    ["processing", "partially_completed"]
                                ),
                                1
                            ),
                            else_=0
                        )
                    )
                )
            )
            p_row = proj_stats.first()
            total_projects = p_row[0] or 0
            active_projects = p_row[1] or 0
        aggregated_products = 0
        cleaned_products = 0
        enriched_products = 0
        published_products = 0
        if project_id and proj:
            operation_mode = (proj.operation_mode or "").lower()
            use_case = (proj.use_case or "").lower()
            if operation_mode == "aggregation":
                aggregated_products = completed_count
            elif operation_mode == "cleaning":
                cleaned_products = completed_count
            elif operation_mode == "enrichment":
                enriched_products = completed_count
            enrichment_stmt = select(func.count(Product.id)).where(
                *filters,
                (
                    (Product.workflow_stage == "enrichment")
                    | (Product.needs_enrichment == True)
                )
            )
            enrichment_workflow_count = (
                (await db.execute(enrichment_stmt)).scalar() or 0
            )
            if operation_mode == "aggregation":
                enriched_products = enrichment_workflow_count
            elif operation_mode == "enrichment":
                enriched_products = max(
                    enriched_products,
                    enrichment_workflow_count
                )
        else:
            aggregated_products = completed_count
            enrichment_workflow_stmt = select(func.count(Product.id)).where(
                *filters,
                (
                    (Product.workflow_stage == "enrichment")
                    | (Product.needs_enrichment == True)
                )
            )
            enriched_products = (
                (await db.execute(enrichment_workflow_stmt)).scalar() or 0
            )
            cleaning_stmt = (
                select(func.count(Product.id))
                .join(Project, Product.project_id == Project.id)
                .where(
                    *filters,
                    Product.enrichment_status == "completed",
                    Project.operation_mode == "cleaning"
                )
            )
            cleaned_products = (
                (await db.execute(cleaning_stmt)).scalar() or 0
            )
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
            "totalBrands": total_brands,
            "totalCategories": total_categories,
        }
    except Exception as e:
        print(f"Error calculating metrics: {str(e)}")
        raise
@router.get("/metrics", response_model=DashboardMetricsResponse)
async def get_dashboard_metrics(
        start_date: Optional[str] = Query(None), end_date: Optional[str] = Query(None), date_field: DateField = Query("created_at"),  mode: Optional[str] = Query(None), db: AsyncSession = Depends(get_session),):
    try:
        start_dt = parse_date(start_date, end=False)
        end_dt = parse_date(end_date, end=True)
        data = await calculate_metrics(db, None, start_dt, end_dt, date_field=date_field,mode=mode)
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
    mode: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_session),
):
    try:
        logger.info(
            f"[DEBUG] get_project_metrics called with project_id: '{project_id}' (type: {type(project_id).__name__})")
        start_dt = parse_date(start_date, end=False)
        end_dt = parse_date(end_date, end=True)
        data = await calculate_metrics(db, project_id, start_dt, end_dt, date_field=date_field, mode=mode)
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
    if len(s) == 10:
        d = date.fromisoformat(s)
        if end:
            return datetime.combine(d, time(23, 59, 59, 999999))
        return datetime.combine(d, time(0, 0, 0))
    dt = datetime.fromisoformat(s)
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
     mode: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_session)
):
    try:
        start_dt = parse_date(start_date, end=False)
        end_dt = parse_date(end_date, end=True)
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
        )
        if mode and mode in ["aggregation", "cleaning", "enrichment"]:
            stmt = stmt.join(Project, Product.project_id == Project.id).where(Project.operation_mode == mode, *filters)
        else:
            stmt = stmt.where(*filters)
        stmt = stmt.group_by(period_expr).order_by(period_expr)
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
    mode: Optional[str] = Query(None),
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
        )
        if mode and mode in ["aggregation", "cleaning", "enrichment"]:
            stmt = stmt.join(Project, Product.project_id == Project.id).where(Project.operation_mode == mode, *filters)
        else:
            stmt = stmt.where(*filters)
        stmt = stmt.group_by(Product.brand_name).order_by(func.count(Product.id).desc())
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
            Product.id,
            Product.attributes,
        ).where(*filters)
        result = await db.execute(stmt)
        rows = result.all()
        brand_map = {}
        for brand, workflow_stage, enrichment_status, product_id, attributes in rows:
            brand_key = brand or "Unknown"
            if brand_key not in brand_map:
                brand_map[brand_key] = {
                    "brand": brand_key,
                    "aggregationAttributes": 0,
                    "enrichmentAttributes": 0,
                    "completedAttributes": 0,
                    "totalAttributes": 0
                }
            attr_count_stmt = select(func.count(ProductAttributeLinkModel.attribute_id)).where(
                ProductAttributeLinkModel.product_id == product_id
            )
            attr_count_result = await db.execute(attr_count_stmt)
            attr_count = attr_count_result.scalar() or 0
            if isinstance(attributes, dict):
                attr_count += len(attributes)
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
            Product.id,
            Product.attributes,
        ).where(*filters)
        result = await db.execute(stmt)
        rows = result.all()
        category_map = {}
        for taxonomy, workflow_stage, enrichment_status, product_id, attributes in rows:
            cat_key = taxonomy or "Uncategorized"
            if cat_key not in category_map:
                category_map[cat_key] = {
                    "category": cat_key,
                    "aggregationAttributes": 0,
                    "enrichmentAttributes": 0,
                    "completedAttributes": 0,
                    "totalAttributes": 0
                }
            attr_count_stmt = select(func.count(ProductAttributeLinkModel.attribute_id).where(
                ProductAttributeLinkModel.product_id == product_id))
            attr_count_result = await db.execute(attr_count_stmt)
            attr_count = attr_count_result.scalar() or 0
            if isinstance(attributes, dict):
                attr_count += len(attributes)
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
     mode: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_session)
):
    try:
        start_dt = parse_date(start_date, end=False)
        end_dt = parse_date(end_date, end=True)
        filters = build_product_filters(project_id, start_dt, end_dt, date_field=date_field)
        u_stmt = select(func.count(Product.id)).where((Product.taxonomy == None) | (Product.taxonomy == ""), *filters)
        p_stmt = select(func.count(Product.id)).where(Product.enrichment_status == "pending", *filters)
        f_stmt = select(func.count(Product.id)).where(Product.enrichment_status == "failed", *filters)
        if mode and mode in ["aggregation", "cleaning", "enrichment"]:
            u_stmt = u_stmt.join(Project, Product.project_id == Project.id).where(Project.operation_mode == mode)
            p_stmt = p_stmt.join(Project, Product.project_id == Project.id).where(Project.operation_mode == mode)
            f_stmt = f_stmt.join(Project, Product.project_id == Project.id).where(Project.operation_mode == mode)
        uncategorized = (await db.execute(u_stmt)).scalar() or 0
        pending = (await db.execute(p_stmt)).scalar() or 0
        failed = (await db.execute(f_stmt)).scalar() or 0
        return {"uncategorized": uncategorized, "invalidAttributes": 0, "pendingAggregation": pending, "failedJobs": failed}
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
        filters = build_product_filters(
            project_id, start_dt, end_dt, date_field=date_field)
        stmt = select(
            Product.product_name,
            Product.product_code,
            Product.enrichment_status,
            Product.id,
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
def normalize_project_status(status: Optional[str]) -> str:
    if status in (None, "", "draft"):
        return "yet_to_start"
    if status == "processing":
        return "in_progress"
    return status
def get_db_status_filters(status: Optional[str]) -> list[str]:
    if not status or status == "all":
        return []
    if status == "yet_to_start":
        return ["yet_to_start", "draft"]
    if status == "in_progress":
        return ["in_progress", "processing"]
    return [status]
@router.get("/projects-overview")
async def get_projects_overview(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = None,
    status: str | None = None,
    db: AsyncSession = Depends(get_session),
):
    try:
        db_statuses = get_db_status_filters(status)
        base_filters = []
        if search:
            base_filters.append(Project.name.ilike(f"%{search}%"))
        if db_statuses:
            base_filters.append(Project.status.in_(db_statuses))
        offset = (page - 1) * page_size
        paged_stmt = (
            select(
                Project.id,
                func.count().over().label("total_count"),
            )
            .where(*base_filters)
            .order_by(Project.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        combined_result = await db.execute(paged_stmt)
        combined_rows = combined_result.all()
        total_count = combined_rows[0].total_count if combined_rows else 0
        project_ids = [row.id for row in combined_rows]
        if not project_ids:
            return {"projects": [], "total": total_count, "page": page, "page_size": page_size}
        stmt = select(
            Project.id,
            Project.name,
            Project.status,
            Project.operation_mode,
            Project.use_case,
            Project.updated_at,
            func.count(Product.id).label("total_products"),
            func.sum(case((Product.enrichment_status == "completed", 1), else_=0)).label(
                "aggregated"),
            func.sum(case((Product.enrichment_status == "failed", 1), else_=0)).label(
                "failed"),
            func.sum(case((Product.enrichment_status == "completed", 1), else_=0)).label(
                "enrichment"),  
            func.sum(case((Product.enrichment_status == "failed", 1), else_=0)).label(
                "enrichment_failed"),  
            func.sum(case((Product.workflow_stage == "cleaning", 1), else_=0)).label(
                "cleaning"),
        ).outerjoin(
            Product, Project.id == Product.project_id
        ).where(
            Project.id.in_(project_ids)
        ).group_by(
            Project.id
        ).order_by(
            Project.created_at.desc()
        )
        result = await db.execute(stmt)
        rows = result.all()
        overview_list = []
        for row in rows:
            total = row.total_products or 0
            aggregated = row.aggregated or 0
            failed = row.failed or 0
            enrichment = row.enrichment or 0
            cleaning = row.cleaning or 0
            overall_pct = round((aggregated / total) * 100) if total > 0 else 0
            overview_list.append(ProjectOverview(
                id=str(row.id),
                name=row.name,
                totalProducts=total,
                aggregated=aggregated,
                aggregationFailed=failed,
                enrichment=enrichment,
                enrichmentFailed=row.enrichment_failed or 0,
                cleaning=cleaning,
                overallPct=overall_pct,
                status=normalize_project_status(row.status),
                lastActive=row.updated_at.strftime(
                    "%Y-%m-%d") if row.updated_at else "Never",
                operationMode=row.operation_mode or "",
                useCase=row.use_case or "",
            ))
        return {"projects": overview_list, "total": total_count, "page": page, "page_size": page_size}
    except Exception as e:
        logger.error(f"Failed to get projects overview: {e}", exc_info=True)
        return {"projects": [], "total": 0, "page": page, "page_size": page_size}
@router.get("/attribute-summary")
async def get_attribute_summary(
    project_id: Optional[str] = Query(None),
    taxonomy: Optional[str] = Query(None), 
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    date_field: DateField = Query("created_at"),
    db: AsyncSession = Depends(get_session)
):
    try:
        # 1. Verify these model names match your actual database models!
        from app.models.attribute import Attribute, AttributeValue
        from app.models.product_attribute_link import ProductAttributeValueLinkModel 

        start_dt = parse_date(start_date, end=False)
        end_dt = parse_date(end_date, end=True)
        
        filters = build_product_filters(project_id, start_dt, end_dt, date_field=date_field)
        
        if taxonomy:
            # Using ilike and trim makes the match much more reliable
            filters.append(func.trim(Product.taxonomy).ilike(taxonomy.strip()))

        # --- DEBUG LOGGING ---
        # First, check if products exist at all for this taxonomy and date
        count_stmt = select(func.count(Product.id)).where(*filters)
        count_res = await db.execute(count_stmt)
        total_prods = count_res.scalar()
        logger.info(f"DEBUG: Found {total_prods} products for taxonomy '{taxonomy}' and dates {start_date} to {end_date}")

        if total_prods == 0:
            return []
        # ---------------------

        stmt = (
            select(
                Attribute.attribute_name,
                func.count(func.distinct(AttributeValue.value)).label("unique_values"),
                func.array_agg(func.distinct(AttributeValue.uom)).label("uoms")
            )
            .select_from(Product) 
            .join(
                ProductAttributeValueLinkModel, 
                ProductAttributeValueLinkModel.product_id == Product.id
            )
            .join(
                AttributeValue, 
                ProductAttributeValueLinkModel.attribute_value_id == AttributeValue.id
            )
            .join(
                Attribute, 
                AttributeValue.attribute_id == Attribute.id
            )
            .where(*filters)
            .group_by(Attribute.attribute_name)
            .order_by(func.count(func.distinct(AttributeValue.value)).desc())
            .limit(40) 
        )

        result = await db.execute(stmt)
        rows = result.all()
        
        logger.info(f"DEBUG: Successfully joined and found {len(rows)} attributes")

        return [
            {
                "attribute_name": row.attribute_name,
                "unique_values": row.unique_values,
                "uoms": [u for u in row.uoms if u]
            }
            for row in rows
        ]
    except Exception as e:
        logger.error(f"Failed to get attribute summary: {e}", exc_info=True)
        return []
    ``
from sqlalchemy.orm import aliased
@router.get("/taxonomies-list")
async def get_taxonomies_list(
    project_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_session)
):
    """
    Returns a list of all taxonomies and their product counts.
    Used for the dropdown selection in the Attributes tab.
    """
    try:
        stmt = select(
            Product.taxonomy,
            func.count(Product.id).label("count")
        ).where(
            Product.taxonomy.isnot(None),
            Product.taxonomy != ""
        )
        if project_id:
            stmt = stmt.where(Product.project_id == project_id)
        stmt = stmt.group_by(Product.taxonomy).order_by(func.count(Product.id).desc())
        result = await db.execute(stmt)
        return [{"taxonomy": row.taxonomy, "count": row.count} for row in result.all()]
    except Exception as e:
        logger.error(f"Failed to get taxonomies list: {e}")
        return []
@router.get("/taxonomy-attribute-metrics")
async def get_taxonomy_attribute_metrics(
    taxonomy: str = Query(...),
    project_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_session)
):
    try:
        from app.models.attribute import Attribute, AttributeValue
        from app.models.product_attribute_link import ProductAttributeValueLinkModel
        count_stmt = select(func.count(Product.id)).where(Product.taxonomy == taxonomy)
        if project_id:
            count_stmt = count_stmt.where(Product.project_id == project_id)
        total_products_res = await db.execute(count_stmt)
        total_products = total_products_res.scalar() or 0
        attr_stmt = (
            select(
                func.count(func.distinct(Attribute.id)).label("attr_count"),
                func.count(func.distinct(AttributeValue.id)).label("value_count")
            )
            .select_from(Product) 
            .join(
                ProductAttributeValueLinkModel, 
                ProductAttributeValueLinkModel.product_id == Product.id
            )
            .join(
                AttributeValue, 
                ProductAttributeValueLinkModel.attribute_value_id == AttributeValue.id
            )
            .join(
                Attribute, 
                AttributeValue.attribute_id == Attribute.id
            )
            .where(Product.taxonomy == taxonomy)
        )
        if project_id:
            attr_stmt = attr_stmt.where(Product.project_id == project_id)
        stats_res = await db.execute(attr_stmt)
        stats = stats_res.first()
        total_attributes = stats.attr_count if stats else 0
        total_values = stats.value_count if stats else 0
        avg_unique = 0
        if total_attributes > 0:
            avg_unique = round(total_values / total_attributes, 1)
        return {
            "totalProducts": total_products,
            "totalAttributes": total_attributes,
            "avgUniqueValues": avg_unique,
            "avgDensity": 0 
        }
    except Exception as e:
        logger.error(f"Failed to get taxonomy attribute metrics: {e}", exc_info=True)
        return {
            "totalProducts": 0,
            "totalAttributes": 0,
            "avgUniqueValues": 0,
            "avgDensity": 0
        }