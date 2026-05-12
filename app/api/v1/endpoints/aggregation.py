from app.utils.timezone import now_ist
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, func, and_, case
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta, timezone
import logging
from sqlalchemy.orm.attributes import flag_modified
from fastapi.responses import StreamingResponse
import asyncio
import traceback
from sqlmodel import update
import io
import pandas as pd
from app.core.config import settings
from app.core.database import get_session, async_session_factory
from app.models.attribute import Attribute, AttributeValue, CategoryAttribute
from app.models.pipeline import AggregationJob, AuditTrail, CleansingIssue, RawExtraction, Source
from app.models.product import Product
from app.models.product_attribute_link import ProductAttributeLinkModel, ProductAttributeValueLinkModel
from app.models.project import Project
from app.aggregation.aggregate_product import aggregate_product, chunk_attributes
from app.schemas.aggregation import AggregateLLMRequest, AggregatedAttribute, AggregatedAttributeValue, AggregationTriggerResponse, BatchExportRequest, ProductAggregationResponse, ProjectStats
from app.utils import llm_usage
from app.utils.aggregate_download import generate_products_excel
from app.utils.llm_usage import track_llm_usage
from app.utils.validators import is_invalid
from app.utils.image_validator import validate_image_url
from app.utils.sanitize import sanitize_ai_data
from app.aggregation.worker_pool import get_worker_pool
logger = logging.getLogger("aggregation_router")
router = APIRouter()


async def update_project_status(db_session: AsyncSession, project_id: str) -> None:
    stmt = select(
        func.count(Product.id).label("total"),
        func.sum(case((Product.enrichment_status == 'completed', 1), else_=0)).label(
            "completed"),
        func.sum(case((Product.enrichment_status == 'failed', 1), else_=0)).label(
            "failed"),
        func.sum(case((Product.enrichment_status == 'pending', 1), else_=0)).label(
            "pending"),
        func.sum(case((Product.enrichment_status == 'processing', 1), else_=0)).label(
            "processing"),
    ).where(Product.project_id == project_id)
    result = await db_session.execute(stmt)
    stats = result.first()
    enrichment_stmt = select(func.count(Product.id)).where(
        and_(
            Product.project_id == project_id,
            Product.workflow_stage == 'enrichment'
        )
    )
    enrichment_result = await db_session.execute(enrichment_stmt)
    enrichment_count = enrichment_result.scalar() or 0
    total = stats.total or 0
    completed = stats.completed or 0
    failed = stats.failed or 0
    pending = stats.pending or 0
    processing = stats.processing or 0
    if processing > 0:
        status_value = "in_progress"
    elif pending > 0 and completed > 0:
        status_value = "partially_completed"
    elif pending == 0 and completed == total and total > 0:
        status_value = "completed"
    elif failed == total and total > 0:
        status_value = "failed"
    elif total > 0 and completed > 0:
        status_value = "partially_completed"
    elif enrichment_count > 0 and total > 0:
        status_value = "partially_completed"
    else:
        status_value = "yet_to_start"
    logger.info(
        f"update_project_status({project_id}) => "
        f"total={total}, completed={completed}, failed={failed}, "
        f"pending={pending}, processing={processing}, status={status_value}"
    )
    await db_session.execute(
        update(Project).where(Project.id ==
                              project_id).values(status=status_value)
    )


async def refresh_project_status(project_id: str) -> None:
    async with async_session_factory() as session:
        await update_project_status(session, project_id)
        await session.commit()
        project = await session.get(Project, project_id)
        if project:
            logger.info(
                f"Refreshed project {project_id} final status: {project.status}"
            )


def merge_attributes_preserving_order(
    primary_attributes: List[str],
    existing_attrs: Dict[str, Any],
    ai_data: Dict[str, Any]
) -> Dict[str, Any]:
    merged = {}
    for attr_name in primary_attributes:
        if attr_name in existing_attrs:
            existing_val = existing_attrs[attr_name]
            merged[attr_name] = existing_val if isinstance(
                existing_val, dict) else existing_val
        elif attr_name in ai_data:
            merged[attr_name] = ai_data[attr_name]
    for attr_name, ai_val in ai_data.items():
        if attr_name not in merged:
            merged[attr_name] = ai_val
    return merged


async def get_active_job_for_project(db: AsyncSession, project_id: str) -> Optional[AggregationJob]:
    stmt = select(AggregationJob).where(
        and_(
            AggregationJob.project_id == project_id,
            AggregationJob.status.in_(['pending', 'processing'])
        )
    )
    result = await db.execute(stmt)
    return result.scalars().first()


@router.get("/projects/stats", response_model=List[ProjectStats])
async def get_projects_with_aggregation_stats(
    db: AsyncSession = Depends(get_session)
) -> List[ProjectStats]:
    try:
        projects_stmt = select(Project).order_by(Project.created_at.desc())
        projects_result = await db.execute(projects_stmt)
        projects = projects_result.scalars().all()
        result: List[ProjectStats] = []
        for project in projects:
            pid = str(project.id)
            stats_stmt = select(
                func.count(Product.id).label('total'),
                func.sum(case((Product.enrichment_status == 'completed', 1), else_=0)).label(
                    'completed'),
                func.sum(case((Product.enrichment_status == 'failed', 1), else_=0)).label(
                    'failed'),
                func.sum(case((Product.enrichment_status == 'pending', 1), else_=0)).label(
                    'pending')
            ).where(Product.project_id == pid)
            stats_res = await db.execute(stats_stmt)
            stats = stats_res.first()
            total = stats.total or 0
            completed = stats.completed or 0
            failed = stats.failed or 0
            pending = stats.pending or 0
            active_job = await get_active_job_for_project(db, pid)
            if active_job:
                agg_status = 'in_progress'
            elif total == 0:
                agg_status = 'yet_to_start'
            elif pending > 0 and completed > 0:
                agg_status = 'partially_completed'
            elif pending == 0 and completed > 0 and failed == 0:
                agg_status = 'completed'
            elif pending == 0 and completed > 0 and failed > 0:
                agg_status = 'partially_completed'
            elif completed == 0 and pending > 0:
                agg_status = 'in_progress'
            elif failed == total:
                agg_status = 'failed'
            else:
                agg_status = 'yet_to_start'
            result.append(ProjectStats(
                id=pid,
                name=project.name,
                client=project.client,
                status=project.status,
                totalProducts=total,
                aggregatedProducts=completed,
                pendingProducts=pending,
                failedProducts=failed,
                aggregationStatus=agg_status
            ))
        return result
    except Exception as e:
        logger.error(f"Stats Error: {e}", exc_info=True)
        return []


@router.post("/project/{project_id}", response_model=AggregationTriggerResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_project_aggregation(
    project_id: str,
    request: AggregateLLMRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session)
) -> AggregationTriggerResponse:
    try:
        project = await db.get(Project, project_id)
        if not project:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found"
            )
        active_job = await get_active_job_for_project(db, project_id)
        if active_job:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Aggregation already in progress. Job ID: {active_job.id}"
            )
        pending_stmt = select(func.count(Product.id)).where(
            and_(
                Product.project_id == project_id,
                Product.enrichment_status.in_(['pending', 'failed'])
            )
        )
        pending_result = await db.execute(pending_stmt)
        pending_count = pending_result.scalar() or 0
        if pending_count == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No pending products to aggregate"
            )
        update_stmt = (
            update(Product)
            .where(
                and_(
                    Product.project_id == project_id,
                    Product.enrichment_status.in_(['pending', 'failed'])
                )
            )
            .values(enrichment_status='processing')
        )
        await db.execute(update_stmt)
        job = AggregationJob(
            project_id=project_id,
            status='pending',
            total_products=pending_count,
            successful=0,
            failed=0,
            started_at=now_ist(),
            details={
                'project_name': project.name,
                'triggered_at': now_ist().isoformat(),
                'llm_provider': request.llm_provider
            }
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
        await db.execute(update(Project).where(Project.id == project_id).values(status='in_progress'))
        # background_tasks.add_task(
        #     run_project_aggregation_task, str(job.id), request.llm_provider)
        background_tasks.add_task(
    run_project_aggregation_task, 
    str(job.id), 
    request.llm_provider, 
    getattr(request, 'missing_llm_provider', None)  # Safe get
)
        logger.info(
            f"Aggregation job {job.id} created for project {project_id} with {pending_count} products")
        return AggregationTriggerResponse(
            status='accepted',
            message=f'Aggregation started for {pending_count} products',
            job_id=str(job.id),
            project_id=project_id,
            total_products=pending_count
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to start project aggregation: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to start aggregation"
        )


@router.post("/project/{project_id}/cancel")
async def cancel_project_aggregation(
    project_id: str,
    db: AsyncSession = Depends(get_session)
) -> Dict[str, str]:
    try:
        active_job = await get_active_job_for_project(db, project_id)
        if not active_job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No active aggregation job found"
            )
        active_job.status = 'cancelled'
        active_job.completed_at = now_ist()
        active_job.error_message = 'Cancelled by user'
        db.add(active_job)
        await db.commit()
        logger.info(
            f"Aggregation job {active_job.id} cancelled for project {project_id}")
        return {
            'status': 'cancelled',
            'message': f'Aggregation job {active_job.id} has been cancelled',
            'job_id': str(active_job.id)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to cancel aggregation: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to cancel aggregation"
        )


@router.post("/run/{product_id}", response_model=ProductAggregationResponse)
async def aggregate_single_product(
    product_id: str,
    request: AggregateLLMRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session)
) -> ProductAggregationResponse:
    try:
        product = await db.get(Product, product_id)
        if not product:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Product not found"
            )
        used_llms = product.used_llms or []
        if request.llm_provider in used_llms:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f'Product has been already enriched with LLM,Please try another one!')
        logger.info(
            f"Starting single product aggregation:{product.product_code}")
        if product.enrichment_status == 'processing':
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Product is already being processed"
            )
        product.enrichment_status = 'processing'
        db.add(product)
        await db.commit()
        # queue_position = await worker_pool.submit(str(product.id), request.llm_provider)
        queue_position = await worker_pool.submit(
    str(product.id), 
    request.llm_provider, 
    getattr(request, 'missing_llm_provider', None)
)

        logger.info(
            f"Queued {product.product_code} at position {queue_position}")
        return ProductAggregationResponse(
            status='accepted',
            product_id=str(product.id),
            attributes_count=0,
            confidence=0.0,
            message=f'Aggregation started for {product.product_code}'
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to start product aggregation: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to start aggregation"
        )


@router.get("/attributes/{product_id}", response_model=List[AggregatedAttribute])
async def get_aggregated_attributes(
    product_id: str,
    db: AsyncSession = Depends(get_session)
) -> List[AggregatedAttribute]:
    try:
        product = await db.get(Product, product_id)
        if not product:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Product not found"
            )
        stmt = select(RawExtraction).where(
            func.json_extract_path_text(
                RawExtraction.product_keys, 'sku') == product.product_code
        )
        result = await db.execute(stmt)
        extractions = result.scalars().all()
        evidence_map: Dict[str, List[AggregatedAttributeValue]] = {}
        for ext in extractions:
            if not isinstance(ext.raw_attributes, dict):
                continue
            for attr_name, attr_val in ext.raw_attributes.items():
                if attr_name not in evidence_map:
                    evidence_map[attr_name] = []
                evidence_map[attr_name].append(AggregatedAttributeValue(
                    value=str(attr_val),
                    confidence=ext.confidence,
                    source_id=str(ext.source_id)[:8]
                ))
        attributes: List[AggregatedAttribute] = []
        current_attrs = product.attributes or {}
        for attr_name, master_value in current_attrs.items():
            values_from_sources = evidence_map.get(attr_name, [])
            unique_values = set(v.value for v in values_from_sources)
            has_conflict = len(unique_values) > 1
            if not values_from_sources:
                values_from_sources = [AggregatedAttributeValue(
                    value=str(master_value),
                    confidence=1.0,
                    source_id="master"
                )]
            attributes.append(AggregatedAttribute(
                id=f"{product_id}_{attr_name}",
                product_id=product_id,
                attribute_name=attr_name,
                has_conflict=has_conflict,
                values=values_from_sources
            ))
        return attributes
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to get aggregated attributes: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch attributes"
        )


def extract_ai_value_text(ai_val: Any) -> str:
    if isinstance(ai_val, dict):
        val = ai_val.get("web_value") or ai_val.get(
            "standard_value") or ai_val.get("value")
        unit = ai_val.get("web_unit") or ai_val.get(
            "uom") or ai_val.get("unit") or ""
        if val is not None:
            return f"{val} {unit}".strip()
    return str(ai_val)
# def merge_dynamic_attributes(
#     product: Product,
#     ai_data: Dict[str, Any],
#     is_validation_mode: bool = False
# ) -> None:
#     existing_names = {attr.get(
#         'name') for attr in product.dynamic_attributes if isinstance(attr, dict)}
#     for attr in product.dynamic_attributes:
#         if not isinstance(attr, dict) or not attr.get('name'):
#             continue
#         attr_name = attr['name']
#         if attr_name not in ai_data:
#             continue
#         ai_val = ai_data[attr_name]
#         if isinstance(ai_val, dict):
#             new_val = ai_val.get('value', '')
#             new_uom = ai_val.get('unit', '') or ai_val.get('uom', '')
#         else:
#             new_val = str(ai_val) if ai_val else ''
#             new_uom = ''
#         if new_val:
#             attr['value'] = new_val
#             if new_uom:
#                 attr['uom'] = new_uom
#             if is_validation_mode:
#                 attr['validation_value'] = new_val
#                 attr['validation_uom'] = new_uom
#     for attr_name, ai_val in ai_data.items():
#         if attr_name in existing_names:
#             continue
#         if isinstance(ai_val, dict):
#             value = ai_val.get('value', '')
#             uom = ai_val.get('unit', '') or ai_val.get('uom', '')
#         else:
#             value = str(ai_val) if ai_val else ''
#             uom = ''
#         if value:
#             product.dynamic_attributes.append({
#                 'name': attr_name,
#                 'value': value,
#                 'uom': uom,
#                 'validation_value': value if is_validation_mode else '',
#                 'validation_uom': uom if is_validation_mode else ''
#             })
#     flag_modified(product, "dynamic_attributes")


async def merge_dynamic_attributes(
    db: AsyncSession,
    product: Product,
    ai_data: Dict[str, Any],
    is_validation_mode: bool = False
) -> None:
    from app.models.attribute import Attribute, AttributeValue
    from app.models.product_attribute_link import ProductAttributeLinkModel, ProductAttributeValueLinkModel
    for attr_name, ai_val in ai_data.items():
        try:
            if isinstance(ai_val, dict):
                value = ai_val.get('value', '')
                uom = ai_val.get('unit', '') or ai_val.get('uom', '')
            else:
                value = str(ai_val) if ai_val else ''
                uom = ''
            if not value:
                continue
            attr_stmt = select(Attribute).where(
                Attribute.attribute_name == attr_name)
            attr_result = await db.execute(attr_stmt)
            attribute = attr_result.scalars().first()
            if not attribute:
                import uuid as _uuid
                base_code = attr_name.lower().replace(
                    " ", "_").replace("/", "_").replace("-", "_")
                from sqlmodel import select as _select
                code_check = await db.execute(
                    _select(Attribute).where(
                        Attribute.attribute_code == base_code)
                )
                if code_check.scalars().first():
                    base_code = f"{base_code}_{_uuid.uuid4().hex[:6]}"
                display_name = attr_name.strip()
                if display_name:
                    display_name = display_name[0].upper(
                    ) + display_name[1:] if len(display_name) > 1 else display_name.upper()
                attribute = Attribute(
                    attribute_name=display_name,
                    attribute_code=base_code,
                    data_type="string",
                )
                db.add(attribute)
                await db.flush()
            link_stmt = select(ProductAttributeLinkModel).where(
                ProductAttributeLinkModel.product_id == product.id,
                ProductAttributeLinkModel.attribute_id == attribute.id,
            )
            if not (await db.execute(link_stmt)).scalars().first():
                db.add(ProductAttributeLinkModel(
                    product_id=product.id, attribute_id=attribute.id))
            val_stmt = select(AttributeValue).where(
                AttributeValue.attribute_id == attribute.id,
                AttributeValue.value == str(value),
            )
            val_result = await db.execute(val_stmt)
            attr_val = val_result.scalars().first()
            if not attr_val:
                attr_val = AttributeValue(
                    attribute_id=attribute.id,
                    value=str(value),
                    uom=str(uom) if uom else None,
                    validation_value=str(
                        value) if is_validation_mode else None,
                    validation_uom=str(
                        uom) if is_validation_mode and uom else None,
                )
                db.add(attr_val)
                await db.flush()
            elif is_validation_mode:
                attr_val.validation_value = str(value)
                attr_val.validation_uom = str(uom) if uom else None
                db.add(attr_val)
            pv_stmt = select(ProductAttributeValueLinkModel).where(
                ProductAttributeValueLinkModel.product_id == product.id,
                ProductAttributeValueLinkModel.attribute_value_id == attr_val.id,
            )
            if not (await db.execute(pv_stmt)).scalars().first():
                db.add(ProductAttributeValueLinkModel(
                    product_id=product.id,
                    attribute_value_id=attr_val.id,
                ))
            if product.category_id:
                ca_stmt = select(CategoryAttribute).where(
                    CategoryAttribute.category_id == product.category_id,
                    CategoryAttribute.attribute_id == attribute.id,
                )
                if not (await db.execute(ca_stmt)).scalars().first():
                    db.add(CategoryAttribute(
                        category_id=product.category_id,
                        attribute_id=attribute.id,
                    ))
        except Exception as e:
            logger.error(
                f"Failed to save attribute {attr_name} to normalized tables: {e}")
            continue


async def get_product_attributes_for_aggregation(
    db: AsyncSession,
    product: Product
) -> tuple[List[str], Dict[str, Dict[str, str]]]:
    """
    Get primary attribute names and existing data for a product.
    Returns: (primary_attr_names, existing_data)
    - primary_attr_names: category attrs + product attrs (deduplicated)
    - existing_data: {attr_name: {value, uom}}
    """
    primary_attr_names = []
    existing_data = {}
    # 1. Get category expected attributes
    if product.category_id:
        try:
            attr_stmt = (
                select(Attribute.attribute_name)
                .join(CategoryAttribute, CategoryAttribute.attribute_id == Attribute.id)
                .where(CategoryAttribute.category_id == product.category_id)
                .order_by(CategoryAttribute.display_order)
            )
            attr_result = await db.execute(attr_stmt)
            for row in attr_result.all():
                if row[0] not in primary_attr_names:
                    primary_attr_names.append(row[0])
        except Exception as e:
            logger.warning(
                f"Failed to get category attrs for {product.product_code}: {e}")
    # 2. Get product's existing attributes
    try:
        val_stmt = (
            select(Attribute.attribute_name,
                   AttributeValue.value, AttributeValue.uom)
            .join(AttributeValue, AttributeValue.attribute_id == Attribute.id)
            .join(ProductAttributeValueLinkModel,
                  ProductAttributeValueLinkModel.attribute_value_id == AttributeValue.id)
            .where(ProductAttributeValueLinkModel.product_id == product.id)
        )
        val_result = await db.execute(val_stmt)
        for attr_name, value, uom in val_result.all():
            existing_data[attr_name] = {'value': value, 'uom': uom or ''}
            if attr_name not in primary_attr_names:
                primary_attr_names.append(attr_name)
    except Exception as e:
        logger.warning(
            f"Failed to read existing attrs for {product.product_code}: {e}")
    return primary_attr_names, existing_data

async def run_project_aggregation_task(job_id: str, llm_provider: str = 'openai', missing_llm_provider: str = None) -> None:
# async def run_project_aggregation_task(job_id: str, llm_provider: str = 'openai') -> None:
    async with async_session_factory() as db_session:
        job: Optional[AggregationJob] = None
        try:
            job = await db_session.get(AggregationJob, job_id)
            if not job:
                logger.error(f"Aggregation job {job_id} not found")
                return
            if job.status == 'cancelled':
                logger.info(f"Job {job_id} was cancelled before processing")
                return
            job.status = 'processing'
            db_session.add(job)
            await db_session.execute(update(Project).where(Project.id == job.project_id).values(status='in_progress'))
            await db_session.commit()
            product_ids = job.details.get('product_ids', [])
            if not product_ids:
                logger.warning(
                    f"No product_ids in job {job_id}, falling back to status query")
                stmt = select(Product).where(
                    and_(
                        Product.project_id == job.project_id,
                        Product.enrichment_status.in_(
                            ['processing', 'pending', 'failed'])
                    )
                )
            else:
                stmt = select(Product).where(Product.id.in_(product_ids))
            result = await db_session.execute(stmt)
            products = result.scalars().all()
            successful = 0
            failed = 0
            total = len(products)
            failed_products: List[Dict[str, str]] = []
            routed_to_enrichment = 0
            ready_for_export = 0
            enrichment_threshold = settings.enrichment_threshold
            logger.info(
                f"Starting aggregation job {job_id} for {total} products")
            for idx, product in enumerate(products):
                await db_session.refresh(job)
                if job.status == 'cancelled':
                    logger.info(f"Job {job_id} cancelled during processing")
                    break
                try:
                    logger.info(
                        f"[Job {job_id}] Aggregating {idx+1}/{total}: {product.product_code}")
                    import gc
                    gc.collect()
                    primary_attrs, _ = await get_product_attributes_for_aggregation(
                        db_session, product
                    )
                    logger.info(f"   └─ Taxonomy: {product.taxonomy}")
                    logger.info(f"   └─ Primary attrs: {primary_attrs}")
                    job.current_product = product.product_code
                    job.successful = successful
                    job.failed = failed
                    db_session.add(job)
                    await db_session.commit()
                    aggregation_result = await aggregate_with_retry(
                        db_session=db_session,
                        mpn=product.product_code,
                        title=product.product_name,
                        sku=product.sku,
                        brand=product.brand_name,
                        taxonomy=product.taxonomy,
                        primary_attributes=primary_attrs,
                        project_id=job.project_id,
                        llm_provider=llm_provider,
                        max_retries=2
                    )
                    if aggregation_result.get('status') == 'success':
                        golden = aggregation_result.get('golden_record', {})
                        ai_attributes = golden.get('attributes', {})
                        product.short_description = golden.get(
                            'short_description') or product.short_description
                        product.long_description = golden.get(
                            'long_description') or product.long_description
                        product.features = golden.get(
                            'features') or product.features
                        project = await db_session.get(Project, job.project_id)
                        use_case = project.use_case.lower() if project and project.use_case else ""
                        if "back filling" in use_case or "validation" in use_case:
                            conflicts = {}
                            ai_data_for_merge = {}
                            # existing_attrs = {}
                            # if product.dynamic_attributes:
                            #     for attr in product.dynamic_attributes:
                            #         if isinstance(attr, dict) and attr.get('name'):
                            #             existing_attrs[attr['name']] = {
                            #                 'value': attr.get('value'),
                            #                 'uom': attr.get('uom') or attr.get('unit')
                            #             }
                            _, existing_attrs = await get_product_attributes_for_aggregation(
                                db_session, product
                            )
                            for ai_key, ai_val in ai_attributes.items():
                                ai_key_clean = str(ai_key).lower().replace(
                                    " ", "").replace("_", "").replace("-", "")
                                target_pk = ai_key
                                for pk in primary_attrs:
                                    if str(pk).lower().replace(" ", "").replace("_", "").replace("-", "") == ai_key_clean:
                                        target_pk = pk
                                        break
                                ai_text_val = extract_ai_value_text(ai_val)
                                user_val = existing_attrs.get(target_pk, {})
                                if isinstance(user_val, dict):
                                    user_val = user_val.get('value', '')
                                else:
                                    user_val = user_val if isinstance(
                                        user_val, str) else ''
                                is_mismatch = False
                                if isinstance(ai_val, dict) and ai_val.get("matches_excel") is False:
                                    is_mismatch = True
                                elif user_val and ai_text_val:
                                    if user_val.lower() != ai_text_val.lower() and user_val.lower() not in ['missing', 'none']:
                                        is_mismatch = True
                                if is_mismatch:
                                    conflicts[target_pk] = ai_text_val
                                    logger.info(
                                        f"Correction found for {target_pk}: '{user_val}' -> '{ai_text_val}'")
                                ai_data_for_merge[target_pk] = ai_val
                            product.attributes = merge_attributes_preserving_order(
                                primary_attributes=primary_attrs,
                                existing_attrs=existing_attrs,
                                ai_data=ai_data_for_merge
                            )
                            product.validation_conflicts = conflicts
                            flag_modified(product, "validation_conflicts")
                            product.enrichment_status = 'completed'
                            product.data_quality_score = 100.0

                            await db_session.flush()
                            db_session.add(product)
                            found_image = aggregation_result.get('image_url')
                            if found_image and isinstance(found_image, str):
                                found_image_str = found_image.strip()
                                if found_image_str:
                                    if await validate_image_url(found_image_str):
                                        product.image_url_1 = found_image_str
                                        logger.info(
                                            f"✓ Valid image found and saved for {product.product_code}: {found_image_str}")
                                    else:
                                        logger.warning(
                                            f"⚠ Image URL invalid for {product.product_code}, not saving")
                                else:
                                    logger.debug(
                                        f"Empty image URL for {product.product_code}")
                            else:
                                logger.warning(
                                    f"⚠ No image found during aggregation of {product.product_code}")
                            product.completeness_score = min(
                                len(ai_attributes) * 5, 100)
                            is_enrichment_attempt = product.workflow_stage == 'enrichment'
                            product.sources_consulted = golden.get(
                                'sources_consulted', [])
                            # if product.completeness_score<enrichment_threshold:
                            #     product.workflow_stage='enrichment'
                            #     product.needs_enrichment=True
                            #     product.ready_for_export=False
                            #     product.routed_to_enrichment_at=now_ist()
                            #     product.enrichment_status='pending'
                            #     routed_to_enrichment+=1
                            # else:
                            missing_attrs = [attr for attr in (primary_attrs or []) if attr not in ai_attributes]
                            new_attrs = [attr for attr in ai_attributes if attr not in set(primary_attrs or [])]
                            has_gaps = bool(missing_attrs or new_attrs)

                            if has_gaps and missing_llm_provider and missing_llm_provider != llm_provider:
                                logger.info(f"Algo 2 triggered for {product.product_code}: missing={missing_attrs}, new={new_attrs}")
                                algo2_primary = list(set(missing_attrs + new_attrs))
                                
                                algo2_result = await aggregate_with_retry(
                                    db_session=db_session,
                                    mpn=product.product_code,
                                    title=product.product_name,
                                    sku=product.sku,
                                    brand=product.brand_name,
                                    taxonomy=product.taxonomy,
                                    primary_attributes=algo2_primary,
                                    project_id=job.project_id,
                                    llm_provider=missing_llm_provider,
                                    max_retries=1
                                )
                                
                                if algo2_result.get('status') == 'success':
                                    algo2_golden = algo2_result.get('golden_record', {})
                                    algo2_attrs = algo2_golden.get('attributes', {})
                                    for key, val in algo2_attrs.items():
                                        if key not in product.attributes or (val and (not isinstance(val, dict) or val.get('value'))):
                                            product.attributes[key] = val
                                    flag_modified(product, "attributes")
                                    product.sources_consulted = list(set(
                                        (product.sources_consulted or []) + algo2_golden.get('sources_consulted', [])
                                    ))
                                    product.completeness_score = min(len(product.attributes) * 5, 100)
                                    logger.info(f"Algo 2 filled {len(algo2_attrs)} attributes for {product.product_code}")
                            product.workflow_stage = 'aggregation'
                            product.needs_enrichment = False
                            product.ready_for_export = True
                            product.enrichment_status = 'completed'
                            product.data_quality_score = 100.0

                            product.routed_to_enrichment_at = None
                            ready_for_export += 1
                            track_llm_usage(product, llm_provider,
                                            is_enrichment_attempt, logger)
                            await db_session.flush()
                            await check_data_quality(db_session, product.product_code, ai_attributes)
                            successful += 1
                            logger.info(
                                f"Aggregated {product.product_code}: {len(ai_attributes)} attributes")
                        else:
                            for key, val in ai_attributes.items():
                                if key not in product.attributes or (val and (not isinstance(val, dict) or val.get('value'))):
                                    product.attributes[key] = val
                            flag_modified(product, "attributes")
                            await merge_dynamic_attributes(db_session, product, ai_attributes, is_validation_mode=False)
                            found_image = aggregation_result.get('image_url')
                            if found_image and isinstance(found_image, str):
                                found_image_str = found_image.strip()
                                if found_image_str:
                                    if await validate_image_url(found_image_str):
                                        product.image_url_1 = found_image_str
                                        logger.info(
                                            f"✓ Valid image saved for {product.product_code}")
                                    else:
                                        logger.warning(
                                            f"⚠ Image invalid for {product.product_code}")
                                else:
                                    logger.debug(
                                        f"Empty image URL for {product.product_code}")
                            else:
                                logger.warning(
                                    f"⚠ No image found for {product.product_code}")
                            product.enrichment_status = 'completed'
                            product.data_quality_score = 100.0

                            product.completeness_score = min(
                                len(ai_attributes) * 5, 100)
                            product.sources_consulted = golden.get(
                                'sources_consulted', [])
                            is_enrichment_attempt = product.workflow_stage == 'enrichment'
                            # if product.completeness_score < enrichment_threshold:
                            #     product.workflow_stage = 'enrichment'
                            #     product.needs_enrichment = True
                            #     product.ready_for_export = False
                            #     product.routed_to_enrichment_at = now_ist()
                            #     product.enrichment_status='pending'
                            #     routed_to_enrichment+=1
                            # else:
                            missing_attrs = [attr for attr in (primary_attrs or []) if attr not in ai_attributes]
                            new_attrs = [attr for attr in ai_attributes if attr not in set(primary_attrs or [])]
                            has_gaps = bool(missing_attrs or new_attrs)

                            if has_gaps and missing_llm_provider and missing_llm_provider != llm_provider:
                                logger.info(f"Algo 2 triggered for {product.product_code}: missing={missing_attrs}, new={new_attrs}")
                                algo2_primary = list(set(missing_attrs + new_attrs))
                                
                                algo2_result = await aggregate_with_retry(
                                    db_session=db_session,
                                    mpn=product.product_code,
                                    title=product.product_name,
                                    sku=product.sku,
                                    brand=product.brand_name,
                                    taxonomy=product.taxonomy,
                                    primary_attributes=algo2_primary,
                                    project_id=job.project_id,
                                    llm_provider=missing_llm_provider,
                                    max_retries=1
                                )
                                
                                if algo2_result.get('status') == 'success':
                                    algo2_golden = algo2_result.get('golden_record', {})
                                    algo2_attrs = algo2_golden.get('attributes', {})
                                    for key, val in algo2_attrs.items():
                                        if key not in product.attributes or (val and (not isinstance(val, dict) or val.get('value'))):
                                            product.attributes[key] = val
                                    flag_modified(product, "attributes")
                                    product.sources_consulted = list(set(
                                        (product.sources_consulted or []) + algo2_golden.get('sources_consulted', [])
                                    ))
                                    product.completeness_score = min(len(product.attributes) * 5, 100)
                                    logger.info(f"Algo 2 filled {len(algo2_attrs)} attributes for {product.product_code}")
                            product.workflow_stage = 'aggregation'
                            product.needs_enrichment = False
                            product.ready_for_export = True
                            product.enrichment_status = 'completed'
                            product.data_quality_score = 100.0

                            product.routed_to_enrichment_at = None
                            ready_for_export += 1
                            track_llm_usage(product, llm_provider,
                                            is_enrichment_attempt, logger)
                            db_session.add(product)
                            await db_session.flush()
                            await check_data_quality(db_session, product.product_code, ai_attributes)
                            successful += 1
                            logger.info(
                                f" Aggregated {product.product_code}: {len(ai_attributes)} attributes")
                    else:
                        product.enrichment_status = 'failed'
                        db_session.add(product)
                        failed += 1
                        failed_products.append({
                            'sku': product.product_code,
                            'error': aggregation_result.get('reason', 'Unknown error')
                        })
                        logger.warning(
                            f" Aggregation failed for {product.product_code}")
                    await asyncio.sleep(5)
                except Exception as e:
                    logger.error(
                        f"Error aggregating {product.product_code}: {e}")
                    product.enrichment_status = 'failed'
                    db_session.add(product)
                    failed += 1
                    failed_products.append({
                        'sku': product.product_code,
                        'error': str(e)
                    })
                    continue
            job.status = 'completed' if job.status != 'cancelled' else 'cancelled'
            job.successful = successful
            job.failed = failed
            job.current_product = None
            job.completed_at = now_ist()
            job.details = {
                **job.details,
                'failed_products': failed_products[:50],
                'completed_at': now_ist().isoformat(),
                'total_processed': total,
                'successful_processed': successful,
                'failed_processed': failed,
                'routed_to_enrichment': routed_to_enrichment,
                'ready_for_export': ready_for_export,
                'enrichment_threshold': enrichment_threshold
            }
            db_session.add(job)
            source_stmt = select(Source).where(
                Source.project_id == job.project_id)
            source_result = await db_session.execute(source_stmt)
            await update_project_status(db_session, job.project_id)
            project = await db_session.get(Project, job.project_id)
            processing_status = project.status if project else 'completed'
            sources = source_result.scalars().all()
            for source in sources:
                new_metadata = dict(
                    source.source_metadata) if source.source_metadata else {}
                new_metadata['processing_status'] = processing_status
                new_metadata['successful'] = successful
                new_metadata['failed'] = failed
                new_metadata['routed_to_enrichment'] = routed_to_enrichment
                new_metadata['ready_for_export'] = ready_for_export
                new_metadata['enrichment_threshold'] = enrichment_threshold
                new_metadata['last_run'] = now_ist().isoformat()
                source.source_metadata = new_metadata
                flag_modified(source, "source_metadata")
                db_session.add(source)
            db_session.add(AuditTrail(
                product_id=f"PROJECT_{job.project_id}",
                stage="aggregation",
                attribute_name="project_aggregation",
                selected_value=processing_status.replace('_', ' ').title(),
                sources_used=f"{total} products",
                reason=f"Aggregated {successful}/{total} products successfully, {failed} failed"
            ))
            await update_project_status(db_session, job.project_id)
            await db_session.commit()
            logger.info(
                f"Job {job_id} complete: {successful}/{total} successful, {failed} failed")
            await refresh_project_status(job.project_id)
        except Exception as e:
            await db_session.rollback()
            logger.error(
                f"Aggregation job {job_id} failed: {e}", exc_info=True)
            if job:
                try:
                    job.status = 'failed'
                    job.error_message = str(e)[:500]
                    job.completed_at = now_ist()
                    db_session.add(job)
                    await db_session.commit()
                    await refresh_project_status(job.project_id)
                except Exception as commit_error:
                    logger.error(
                        f"Failed to update job status: {commit_error}")

async def run_single_product_aggregation(product_id: str, llm_provider: str = 'openai', missing_llm_provider: str = None) -> None:
# async def run_single_product_aggregation(product_id: str, llm_provider: str = 'openai') -> None:
    async with async_session_factory() as db_session:
        try:
            product = await db_session.get(Product, product_id)
            if not product:
                logger.error(f"Product {product_id} not found")
                return
            logger.info(
                f"Starting single product aggregation: {product.product_code}")
            is_enrichment_attempt = product.workflow_stage == 'enrichment'
            logger.info(
                f"Product {product.product_code} - workflow_stage: {product.workflow_stage}, is_enrichment_attempt: {is_enrichment_attempt}")
            primary_attrs, _ = await get_product_attributes_for_aggregation(
                db_session, product
            )
            logger.info(f"   └─ Taxonomy: {product.taxonomy}")
            logger.info(f"   └─ Primary attrs: {primary_attrs}")
            if len(primary_attrs) > 10:
                logger.info(
                    f"Product has {len(primary_attrs)} attributes - using multi-pass processing")
                attr_chunks = chunk_attributes(primary_attrs, chunk_size=10)
                merged_ai_data = {}
                all_sources = []
                image_url = None
                short_desc = None
                long_desc = None
                features = None
                for idx, chunk in enumerate(attr_chunks, 1):
                    logger.info(
                        f"   └─ Pass {idx}/{len(attr_chunks)}: Processing attributes {chunk}")
                    chunk_result = await aggregate_with_retry(
                        db_session=db_session,
                        mpn=product.product_code,
                        title=product.product_name,
                        sku=product.sku,
                        upc=product.upc,
                        max_retries=2,
                        brand=product.brand_name,
                        taxonomy=product.taxonomy,
                        primary_attributes=primary_attrs,
                        attribute_chunk=chunk,
                        project_id=product.project_id,
                        llm_provider=llm_provider
                    )
                    if idx < len(attr_chunks):
                        await asyncio.sleep(5)
                    if chunk_result.get('status') == 'success':
                        golden = chunk_result.get('golden_record', {})
                        chunk_attrs = golden.get('attributes', {})
                        merged_ai_data.update(chunk_attrs)
                        sources = golden.get('sources_consulted', [])
                        all_sources.extend(sources)
                        if not image_url and chunk_result.get('image_url'):
                            image_url = chunk_result.get('image_url')
                            logger.info(
                                f"Image captured from pass {idx} {image_url}")
                        if not short_desc and golden.get('short_description'):
                            short_desc = golden.get('short_description')
                        if not long_desc and golden.get('long_description'):
                            long_desc = golden.get('long_description')
                        if not features and golden.get('features'):
                            features = golden.get('features')
                    await asyncio.sleep(1)
                result = {
                    'status': 'success' if merged_ai_data else 'failed',
                    'golden_record': {
                        'attributes': merged_ai_data,
                        'sources_consulted': list(set(all_sources)),
                        'short_description': short_desc or product.short_description,
                        'long_description': long_desc or product.long_description,
                        'features': features or product.features
                    },
                    'image_url': image_url
                }
                logger.info(
                    f" Multi-pass complete: {len(merged_ai_data)} total attributes from {len(attr_chunks)} passes")
            else:
                result = await aggregate_with_retry(
                    db_session=db_session,
                    mpn=product.product_code,
                    title=product.product_name,
                    sku=product.sku,
                    upc=product.upc,
                    max_retries=3,
                    brand=product.brand_name,
                    taxonomy=product.taxonomy,
                    primary_attributes=primary_attrs,
                    project_id=product.project_id,
                    llm_provider=llm_provider
                )
            if result.get('status') == 'success':
                golden = sanitize_ai_data(result.get('golden_record', {}))
                ai_data = golden.get('attributes', {})
                product.short_description = golden.get(
                    'short_description') or product.short_description
                product.long_description = golden.get(
                    'long_description') or product.long_description
                product.features = golden.get('features') or product.features
                project = await db_session.get(Project, product.project_id)
                use_case = project.use_case.lower() if project.use_case else ""
                enrichment_threshold = settings.enrichment_threshold
                if 'back filling' in use_case.lower() or 'validation' in use_case.lower():
                    conflicts = {}
                    ai_data_for_merge = {}
                    _, existing_attrs = await get_product_attributes_for_aggregation(
                        db_session, product
                    )
                    for attr_name, ai_val in ai_data.items():
                        if isinstance(ai_val, dict) and ai_val.get("matches_excel") is False:
                            conflicts[attr_name] = extract_ai_value_text(
                                ai_val)
                        ai_data_for_merge[attr_name] = ai_val
                    product.attributes = merge_attributes_preserving_order(
                        primary_attributes=primary_attrs,
                        existing_attrs=existing_attrs,
                        ai_data=ai_data_for_merge
                    )
                    product.validation_conflicts = conflicts
                    flag_modified(product, "validation_conflicts")
                    await merge_dynamic_attributes(
                        db_session, product, ai_data_for_merge, is_validation_mode=('validation' in use_case))
                    product.enrichment_status = 'completed'
                    product.data_quality_score = 100.0

                    await db_session.flush()
                    db_session.add(product)
                    existing_image = product.image_url_1
                    found_image = result.get('image_url')
                    if found_image and isinstance(found_image, str):
                        found_image_str = found_image.strip()
                        if found_image_str:
                            if await validate_image_url(found_image_str):
                                product.image_url_1 = found_image_str
                                logger.info(
                                    f"✓ Valid image saved for {product.product_code}")
                            else:
                                if existing_image:
                                    product.image_url_1 = existing_image
                                    logger.warning(
                                        f"⚠ New image invalid, keeping existing for {product.product_code}")
                                else:
                                    logger.warning(
                                        f"⚠ Image validation failed and no existing image for {product.product_code}")
                        else:
                            logger.debug(
                                f"Empty image URL for {product.product_code}")
                    else:
                        logger.warning(
                            f"⚠ No image found during single product aggregation of {product.product_code}")
                    product.completeness_score = min(len(ai_data) * 5, 100)
                    product.sources_consulted = golden.get(
                        'sources_consulted', [])
                    # if product.completeness_score < enrichment_threshold:
                    #     product.workflow_stage = 'enrichment'
                    #     product.needs_enrichment = True
                    #     product.ready_for_export = False
                    #     product.routed_to_enrichment_at = now_ist()
                    #     product.enrichment_status = 'pending'
                    # else:
                    missing_attrs = [attr for attr in (primary_attrs or []) if attr not in ai_data]
                    new_attrs = [attr for attr in ai_data if attr not in set(primary_attrs or [])]
                    has_gaps = bool(missing_attrs or new_attrs)

                    if has_gaps and missing_llm_provider and missing_llm_provider != llm_provider:
                        logger.info(f"Algo 2 triggered for {product.product_code}: missing={missing_attrs}, new={new_attrs}")
                        algo2_primary = list(set(missing_attrs + new_attrs))
                        
                        algo2_result = await aggregate_with_retry(
                            db_session=db_session,
                            mpn=product.product_code,
                            title=product.product_name,
                            sku=product.sku,
                            upc=product.upc,
                            brand=product.brand_name,
                            taxonomy=product.taxonomy,
                            primary_attributes=algo2_primary,
                            project_id=product.project_id,
                            llm_provider=missing_llm_provider,
                            max_retries=1
                        )
                        
                        if algo2_result.get('status') == 'success':
                            algo2_golden = algo2_result.get('golden_record', {})
                            algo2_attrs = algo2_golden.get('attributes', {})
                            
                            # Merge Algo 2 results
                            for key, val in algo2_attrs.items():
                                if key not in product.attributes or (val and (not isinstance(val, dict) or val.get('value'))):
                                    product.attributes[key] = val
                            flag_modified(product, "attributes")
                            
                            product.sources_consulted = list(set(
                                (product.sources_consulted or []) + 
                                algo2_golden.get('sources_consulted', [])
                            ))
                            product.completeness_score = min(len(product.attributes) * 5, 100)
                            logger.info(f"Algo 2 filled {len(algo2_attrs)} attributes for {product.product_code}")
                    product.workflow_stage = 'aggregation'
                    product.needs_enrichment = False
                    product.ready_for_export = True
                    product.enrichment_status = 'completed'
                    product.data_quality_score = 100.0

                    product.routed_to_enrichment_at = None
                    track_llm_usage(product, llm_provider,
                                    is_enrichment_attempt, logger)
                    await check_data_quality(db_session, product.product_code, ai_data)
                    logger.info(
                        f"Single product aggregation complete: {product.product_code}")
                else:
                    for key, val in ai_data.items():
                        if key not in product.attributes or (val and (not isinstance(val, dict) or val.get('value'))):
                            product.attributes[key] = val
                    flag_modified(product, "attributes")
                    await merge_dynamic_attributes(db_session,
                                                   product, ai_data, is_validation_mode=False)
                    found_image = result.get('image_url')
                    if found_image and isinstance(found_image, str) and found_image.strip():
                        if await validate_image_url(found_image.strip()):
                            product.image_url_1 = found_image.strip()
                            logger.info(
                                f"✓ Valid image saved for {product.product_code}")
                        else:
                            logger.warning(
                                f"⚠ Image invalid for {product.product_code}")
                    else:
                        logger.warning(
                            f"⚠ No image found for {product.product_code}")
                    product.enrichment_status = 'completed'
                    product.data_quality_score = 100.0

                    product.completeness_score = min(len(ai_data) * 5, 100)
                    product.sources_consulted = golden.get(
                        'sources_consulted', [])
                    # if product.completeness_score < enrichment_threshold:
                    #     product.workflow_stage = 'enrichment'
                    #     product.needs_enrichment = True
                    #     product.ready_for_export = False
                    #     product.routed_to_enrichment_at = now_ist()
                    #     product.enrichment_status = 'pending'
                    # else:
                    missing_attrs = [attr for attr in (primary_attrs or []) if attr not in ai_data]
                    new_attrs = [attr for attr in ai_data if attr not in set(primary_attrs or [])]
                    has_gaps = bool(missing_attrs or new_attrs)

                    if has_gaps and missing_llm_provider and missing_llm_provider != llm_provider:
                        logger.info(f"Algo 2 triggered for {product.product_code}: missing={missing_attrs}, new={new_attrs}")
                        algo2_primary = list(set(missing_attrs + new_attrs))
                        
                        algo2_result = await aggregate_with_retry(
                            db_session=db_session,
                            mpn=product.product_code,
                            title=product.product_name,
                            sku=product.sku,
                            brand=product.brand_name,
                            taxonomy=product.taxonomy,
                            primary_attributes=algo2_primary,
                            project_id=product.project_id,
                            llm_provider=missing_llm_provider,
                            max_retries=1
                        )
                        
                        if algo2_result.get('status') == 'success':
                            algo2_golden = algo2_result.get('golden_record', {})
                            algo2_attrs = algo2_golden.get('attributes', {})
                            for key, val in algo2_attrs.items():
                                if key not in product.attributes or (val and (not isinstance(val, dict) or val.get('value'))):
                                    product.attributes[key] = val
                            flag_modified(product, "attributes")
                            product.sources_consulted = list(set(
                                (product.sources_consulted or []) + algo2_golden.get('sources_consulted', [])
                            ))
                            product.completeness_score = min(len(product.attributes) * 5, 100)
                            logger.info(f"Algo 2 filled {len(algo2_attrs)} attributes for {product.product_code}")
                    product.workflow_stage = 'aggregation'
                    product.needs_enrichment = False
                    product.ready_for_export = True
                    product.enrichment_status = 'completed'
                    product.data_quality_score = 100.0
                    product.routed_to_enrichment_at = None
                    track_llm_usage(product, llm_provider,
                                    is_enrichment_attempt, logger)
                await update_project_status(db_session, product.project_id)
                project = await db_session.get(Project, product.project_id)
                if project:
                    await db_session.refresh(project)
                    logger.info(
                        f"Project {product.project_id} status updated to {project.status}"
                    )
                    if project.status == 'completed':
                        source_stmt = select(Source).where(
                            Source.project_id == product.project_id
                        )
                        source_result = await db_session.execute(source_stmt)
                        sources = source_result.scalars().all()
                        for source in sources:
                            new_metadata = dict(
                                source.source_metadata) if source.source_metadata else {}
                            processing_status = project.status if project else 'completed'
                            new_metadata['processing_status'] = processing_status
                            new_metadata['completed_at'] = now_ist(
                            ).isoformat()
                            source.source_metadata = new_metadata
                            flag_modified(source, "source_metadata")
                            db_session.add(source)
                            logger.info(
                                f"Updated source {source.id} metadata: {new_metadata}"
                            )
                        db_session.add(AuditTrail(
                            product_id=f"PROJECT_{product.project_id}",
                            stage="aggregation",
                            attribute_name="project_completion",
                            selected_value="Completed",
                            sources_used="All products",
                            reason="All products aggregated successfully"
                        ))
                    elif project.status == 'partially_completed':
                        db_session.add(AuditTrail(
                            product_id=f"PROJECT_{product.project_id}",
                            stage="aggregation",
                            attribute_name="project_completion",
                            selected_value="Partially Completed",
                            sources_used="Mixed product states",
                            reason="Some products completed while others are pending for enrichment"
                        ))
                logger.info(
                    f"Single product aggregation complete: {product.product_code}")
            else:
                product.enrichment_status = 'failed'
                failure_reason = result.get('reason', 'Unknown Error')
                logger.error(
                    f"Single product aggregation failed: {product.product_code}. Reason: {failure_reason}")
            db_session.add(product)
            await db_session.commit()
            await db_session.refresh(product)
            logger.info(
                f" Single product saved with image: {product.image_url_1}")
            await refresh_project_status(product.project_id)
            await asyncio.sleep(2)
        except Exception as e:
            await db_session.rollback()
            logger.error(
                f"Single product aggregation failed: {e}", exc_info=True)
            logger.error(f"Traceback: {traceback.format_exc()}")
            try:
                product = await db_session.get(Product, product_id)
                if product:
                    product.enrichment_status = 'failed'
                    db_session.add(product)
                    await db_session.commit()
                    await refresh_project_status(product.project_id)
            except Exception:
                pass
worker_pool = get_worker_pool(process_function=run_single_product_aggregation)


async def aggregate_with_retry(
    db_session,
    mpn: str,
    title: str,
    sku: str,
    upc: Optional[str] = None,
    brand: Optional[str] = None,
    taxonomy: Optional[str] = None,
    primary_attributes: Optional[List[str]] = None,
    attribute_chunk: Optional[List[str]] = None,
    project_id: str = None,
    llm_provider: str = "openai",
    max_retries: int = 2,
    retry_delay: float = 2.0,
) -> Dict[str, Any]:
    last_error: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            result = await aggregate_product(
                db=db_session,
                mpn=mpn,
                sku=sku,
                upc=upc,
                title=title,
                brand=brand,
                taxonomy=taxonomy,
                primary_attributes=primary_attributes,
                attribute_chunk=attribute_chunk,
                project_id=project_id,
                llm_provider=llm_provider
            )
            logger.info(f"Aggregation result for {mpn}: {result}")
            image_url = result.get('golden_record', {}).get('image_url')
            logger.info(f" Image URL in result: {image_url}")
            return result
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                delay = retry_delay * (2 ** attempt)
                logger.warning(
                    f"Aggregation attempt {attempt + 1} failed for {mpn}, retrying in {delay}s: {e}")
                await asyncio.sleep(delay)
            else:
                logger.error(
                    f"All {max_retries + 1} aggregation attempts failed for {mpn}: {e}")
    return {
        'status': 'failed',
        'reason': str(last_error) if last_error else 'Unknown error'
    }


async def check_data_quality(
    db_session: AsyncSession,
    product_code: str,
    ai_data: Dict[str, Any]
) -> None:
    for attr_name, attr_value in ai_data.items():
        val_str = str(attr_value)
        if is_invalid(val_str):
            db_session.add(CleansingIssue(
                product_id=product_code,
                attribute_name=attr_name,
                issue_type='invalid',
                details=f"Placeholder or invalid value detected: '{val_str}'",
                resolved=False
            ))


@router.post('/export/batch', status_code=200)
async def batch_export_products(request: BatchExportRequest, db: AsyncSession = Depends(get_session)):
    try:
        product_id_set = set(request.product_ids)
        # Explicitly preserve the project IDs the frontend asked for
        requested_project_ids = list(
            request.project_ids) if request.project_ids else []
        if request.project_ids:
            stmt = select(Product.id).where(
                Product.project_id.in_(request.project_ids))
            result = await db.execute(stmt)
            product_ids_from_projects = result.scalars().all()
            product_id_set.update(product_ids_from_projects)
        if not product_id_set:
            raise HTTPException(status_code=400, detail='No products selected')
        stmt = select(Product).where(Product.id.in_(list(product_id_set)))
        result = await db.execute(stmt)
        products = result.scalars().all()
        if not products:
            raise HTTPException(status_code=404, detail='No products found')
        # All project IDs involved (from frontend or deduced)
        all_project_ids = (
            requested_project_ids if requested_project_ids
            else list(set(str(p.project_id) for p in products if p.project_id))
        )
        logger.info(
            f"Export request: frontend sent {request.project_ids}, "
            f"deduced project_ids={all_project_ids}, products_count={len(products)}"
        )
        filename = "selected_export"
        # Only use the source filename when the frontend explicitly selected ONE project
        if len(all_project_ids) == 1 and len(requested_project_ids) <= 1:
            source_stmt = select(Source).where(
                Source.project_id == all_project_ids[0]
            ).order_by(Source.uploaded_at.desc())
            source_result = await db.execute(source_stmt)
            source = source_result.scalars().first()
            if source and source.source_url:
                import re
                import os
                raw_name = source.source_url.strip()
                # Keep only the file name (ignore any path)
                raw_name = os.path.basename(raw_name)
                # Remove extension
                clean = re.sub(r'\.[^/.]+$', '', raw_name)
                if clean:
                    filename = clean
                    logger.info(
                        f"Single project export, filename from source: {filename}")
        logger.info(f"Final export filename: {filename}.xlsx")
        return await generate_products_excel(products, db, filename=filename)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'Batch export failed {e}')
        raise HTTPException(
            status_code=500, detail='Failed to download results!')


@router.get("/project/{project_id}/products-with-movement")
async def get_products_with_movement(
    project_id: str,
    db: AsyncSession = Depends(get_session)
) -> Dict[str, Any]:
    try:
        aggregation_stmt = select(Product).where(
            and_(
                Product.project_id == project_id,
                Product.workflow_stage == 'aggregation',
                Product.enrichment_status.in_(
                    ['pending', 'processing', 'failed', 'completed'])
            )
        )
        enrichment_stmt = select(Product).where(
            and_(
                Product.project_id == project_id,
                Product.workflow_stage == 'enrichment',
                Product.enrichment_status.in_(
                    ['pending', 'processing', 'failed'])
            )
        )
        aggregation_result = await db.execute(aggregation_stmt)
        enrichment_result = await db.execute(enrichment_stmt)
        aggregation_products = aggregation_result.scalars().all()
        enrichment_products = enrichment_result.scalars().all()
        completed_products = []
        for product in aggregation_products + enrichment_products:
            if product.enrichment_status == 'completed':
                completed_products.append({
                    "id": str(product.id),
                    "product_code": product.product_code,
                    "product_name": product.product_name,
                    "completeness_score": product.completeness_score,
                    "workflow_stage": product.workflow_stage,
                    "moved_to": 'aggregation' if product.completeness_score >= 90 else 'enrichment'
                })
        latest_product_time = None
        all_products = list(aggregation_products) + list(enrichment_products)
        if all_products:
            latest_product_time = max(
                (p.updated_at for p in all_products if p.updated_at),
                default=None
            )

        return {
            "aggregation_products": [p.dict() for p in aggregation_products],
            "enrichment_products": [p.dict() for p in enrichment_products],
            "completed_products": completed_products,
            "last_updated": latest_product_time.isoformat() if latest_product_time else now_ist().isoformat()
        }
    except Exception as e:
        logger.error(f"Failed to get products with movement: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
@router.get("/project/{project_id}/status")
async def get_project_aggregation_status(
    project_id: str,
    db: AsyncSession = Depends(get_session)
):
    from app.models.pipeline import AggregationJob
    
    stmt = select(AggregationJob).where(
        AggregationJob.project_id == project_id
    ).order_by(AggregationJob.created_at.desc()).limit(1)
    
    result = await db.execute(stmt)
    job = result.scalars().first()
    
    if not job:
        return {"status": "not_found", "error": "No aggregation job found"}
    
    return {"status": job.status, "error": job.error_message}
