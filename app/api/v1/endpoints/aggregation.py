from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, func, and_, case
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import logging
from sqlalchemy.orm.attributes import flag_modified
from fastapi.responses import StreamingResponse
import asyncio
import traceback
from sqlmodel import update
import io
import pandas as pd
from app import llm
from app.core.database import get_session, async_session_factory
from app.models.pipeline import AggregationJob, AuditTrail, CleansingIssue, RawExtraction, Source
from app.models.product import Product
from app.models.project import Project
from app.aggregation.aggregate_product import aggregate_product, chunk_attributes
from app.schemas.aggregation import AggregateLLMRequest, AggregatedAttribute, AggregatedAttributeValue, AggregationJobResponse, AggregationTriggerResponse, BatchExportRequest, ProductAggregationResponse, ProjectStats
from app.utils.aggregate_download import generate_products_excel
from app.utils.validators import is_invalid
from app.utils.image_validator import validate_image_url
from app.utils.sanitize import sanitize_ai_data
from app.aggregation.worker_pool import get_worker_pool

logger = logging.getLogger("aggregation_router")

router = APIRouter()
def merge_attributes_preserving_order(
    primary_attributes: List[str],
    existing_attrs: Dict[str, Any],
    ai_data: Dict[str, Any]
) -> Dict[str, Any]:
    merged = {}
    for attr_name in primary_attributes:
        if attr_name in existing_attrs:
            existing_val = existing_attrs[attr_name]
            merged[attr_name] = existing_val if isinstance(existing_val, dict) else existing_val
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
async def cleanup_old_jobs(db: AsyncSession, days: int = 7) -> int:
    cutoff_date = datetime.utcnow() - timedelta(days=days)
    stmt = select(AggregationJob).where(
        and_(
            AggregationJob.status.in_(['completed', 'failed', 'cancelled']),
            AggregationJob.completed_at < cutoff_date
        )
    )
    result = await db.execute(stmt)
    old_jobs = result.scalars().all()
    count = 0
    for job in old_jobs:
        await db.delete(job)
        count += 1
    if count > 0:
        await db.commit()
        logger.info(f"Cleaned up {count} old aggregation jobs")
    return count
def calculate_progress(job: AggregationJob) -> float:
    if job.total_products == 0:
        return 0.0
    processed = job.successful + job.failed
    return round((processed / job.total_products) * 100, 2)
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
            elif pending == 0 and completed > 0:
                agg_status = 'completed'
            elif completed > 0:
                agg_status = 'in_progress'
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
                Product.enrichment_status.in_(['pending','failed'])
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
            started_at=datetime.utcnow(),
            details={
                'project_name': project.name,
                'triggered_at': datetime.utcnow().isoformat(),
                'llm_provider': request.llm_provider
            }
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
        background_tasks.add_task(run_project_aggregation_task, str(job.id),request.llm_provider )
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
@router.get("/project/{project_id}/status", response_model=AggregationJobResponse)
async def get_project_aggregation_status(
    project_id: str,
    db: AsyncSession = Depends(get_session)
) -> AggregationJobResponse:
    try:
        stmt = select(AggregationJob).where(
            AggregationJob.project_id == project_id
        ).order_by(AggregationJob.created_at.desc()).limit(1)
        result = await db.execute(stmt)
        job = result.scalars().first()
        if not job:
            return AggregationJobResponse(
                id='',
                project_id=project_id,
                status='pending',
                total_products=0,
                successful=0,
                failed=0,
                progress_percent=0.0
            )
        return AggregationJobResponse(
            id=str(job.id),
            project_id=job.project_id,
            status=job.status,
            total_products=job.total_products,
            successful=job.successful,
            failed=job.failed,
            current_product=job.current_product,
            error_message=job.error_message,
            started_at=job.started_at,
            completed_at=job.completed_at,
            progress_percent=calculate_progress(job)
        )
    except Exception as e:
        logger.error(f"Failed to get aggregation status: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch aggregation status"
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
        active_job.completed_at = datetime.utcnow()
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
    request:AggregateLLMRequest,
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
        if product.enrichment_status == 'processing':
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Product is already being processed"
            )
        product.enrichment_status = 'processing'
        db.add(product)
        await db.commit()
        queue_position = await worker_pool.submit(str(product.id),request.llm_provider)
        logger.info(f"Queued {product.product_code} at position {queue_position}")
        # background_tasks.add_task(
        #     run_single_product_aggregation, str(product.id))
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
        val = ai_val.get("web_value") or ai_val.get("standard_value") or ai_val.get("value")
        unit = ai_val.get("web_unit") or ai_val.get("uom") or ai_val.get("unit") or ""
        if val is not None:
            return f"{val} {unit}".strip()
    return str(ai_val)
def merge_dynamic_attributes(
    product: Product,
    ai_data: Dict[str, Any],
    is_validation_mode: bool = False
) -> None:
    """Merge AI‑extracted attributes into product.dynamic_attributes.
    - Updates existing attributes with non‑empty values.
    - Appends new attributes not already present.
    - If is_validation_mode, also sets validation_value/_uom.
    """
    existing_names = {attr.get('name') for attr in product.dynamic_attributes if isinstance(attr, dict)}
    
    # Update existing attributes
    for attr in product.dynamic_attributes:
        if not isinstance(attr, dict) or not attr.get('name'):
            continue
        attr_name = attr['name']
        if attr_name not in ai_data:
            continue
        ai_val = ai_data[attr_name]
        if isinstance(ai_val, dict):
            new_val = ai_val.get('value', '')
            new_uom = ai_val.get('unit', '') or ai_val.get('uom', '')
        else:
            new_val = str(ai_val) if ai_val else ''
            new_uom = ''
        # Only update if new value is non‑empty (to avoid losing existing data)
        if new_val:
            attr['value'] = new_val
            if new_uom:
                attr['uom'] = new_uom
            if is_validation_mode:
                attr['validation_value'] = new_val
                attr['validation_uom'] = new_uom
    
    # Add new attributes
    for attr_name, ai_val in ai_data.items():
        if attr_name in existing_names:
            continue
        if isinstance(ai_val, dict):
            value = ai_val.get('value', '')
            uom = ai_val.get('unit', '') or ai_val.get('uom', '')
        else:
            value = str(ai_val) if ai_val else ''
            uom = ''
        if value:   # only add if there is a value
            product.dynamic_attributes.append({
                'name': attr_name,
                'value': value,
                'uom': uom,
                'validation_value': value if is_validation_mode else '',
                'validation_uom': uom if is_validation_mode else ''
            })
    
    flag_modified(product, "dynamic_attributes")
    
async def run_project_aggregation_task(job_id: str,llm_provider:str='openai') -> None:
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
            await db_session.commit()
            product_ids = job.details.get('product_ids', [])
            if not product_ids:
                logger.warning(f"No product_ids in job {job_id}, falling back to status query")
                stmt = select(Product).where(
                    and_(
                        Product.project_id == job.project_id,
                        Product.enrichment_status.in_(['processing', 'pending', 'failed'])
                    )
                )
            else:
                stmt=select(Product.where(Product.id.in_(product_ids)))
            # stmt = select(Product).where(
            #     and_(
            #         Product.project_id == job.project_id,
            #         Product.enrichment_status.in_ (['pending','failed'])
            #     )
            # )
            result = await db_session.execute(stmt)
            products = result.scalars().all()
            successful = 0
            failed = 0
            total = len(products)
            failed_products: List[Dict[str, str]] = []
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
                        primary_attrs = []
                        if product.dynamic_attributes:
                            for attr in product.dynamic_attributes:
                                attr_name = attr.get('name')
                                if attr_name and str(attr_name).strip():
                                    primary_attrs.append(str(attr_name).strip())
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
                                existing_attrs={}
                                if product.dynamic_attributes:
                                    for attr in product.dynamic_attributes:
                                        if isinstance(attr, dict) and attr.get('name'):
                                            existing_attrs[attr['name']] = {
                    'value': attr.get('value'),
                    'uom': attr.get('uom') or attr.get('unit')
                }
                                for ai_key, ai_val in ai_attributes.items():
                                    ai_key_clean = str(ai_key).lower().replace(" ", "").replace("_", "").replace("-", "")
                                    target_pk = ai_key
                                    for pk in primary_attrs:
                                        if str(pk).lower().replace(" ", "").replace("_", "").replace("-", "") == ai_key_clean:
                                            target_pk = pk
                                            break
                                    ai_text_val = extract_ai_value_text(ai_val)
                                    # user_val = existing_attrs.get(target_pk, "")
                                    user_val=existing_attrs.get(target_pk,{})
                                    if isinstance(user_val,dict):
                                        user_val=user_val.get('value','')
                                    else:
                                        user_val=user_val if isinstance(user_val, str) else ''
                                    is_mismatch = False
                                    if isinstance(ai_val, dict) and ai_val.get("matches_excel") is False:
                                        is_mismatch = True
                                    elif user_val and ai_text_val:
                                        if user_val.lower() != ai_text_val.lower() and user_val.lower() not in ['missing', 'none']:
                                            is_mismatch = True
                                    if is_mismatch:
                                        conflicts[target_pk] = ai_text_val
                                        logger.info(f"Correction found for {target_pk}: '{user_val}' -> '{ai_text_val}'")
                                    ai_data_for_merge[target_pk] = ai_val
                                product.attributes = merge_attributes_preserving_order(
                                    primary_attributes=primary_attrs,
                                    existing_attrs=existing_attrs,
                                    ai_data=ai_data_for_merge
                                )
                                product.validation_conflicts = conflicts
                                flag_modified(product, "validation_conflicts")
                                # if "validation" in use_case and product.dynamic_attributes:
                                #     for attr in product.dynamic_attributes:
                                #         if isinstance(attr, dict) and attr.get('name'):
                                #             attr_name = attr['name']
                                #             if attr_name in ai_data_for_merge:
                                #                 ai_val = ai_data_for_merge[attr_name]
                                #                 if isinstance(ai_val, dict):
                                #                     attr['validation_value'] = ai_val.get('value', '')
                                #                     attr['validation_uom'] = ai_val.get('unit', '') or ai_val.get('uom', '')
                                #                 else:
                                #                     attr['validation_value'] = str(ai_val) if ai_val else ''
                                #     flag_modified(product, "dynamic_attributes")
                                product.enrichment_status = 'completed'
                                await db_session.flush()
                                db_session.add(product)
                                found_image = aggregation_result.get('image_url')
                                if found_image and isinstance(found_image, str):
                                    found_image_str = found_image.strip()
                                    if found_image_str:
                                        # Validate before saving
                                        if await validate_image_url(found_image_str):
                                            product.image_url_1 = found_image_str
                                            logger.info(f"✓ Valid image found and saved for {product.product_code}: {found_image_str}")
                                        else:
                                            logger.warning(f"⚠ Image URL invalid for {product.product_code}, not saving")
                                    else:
                                        logger.debug(f"Empty image URL for {product.product_code}")
                                else:
                                    logger.warning(f"⚠ No image found during aggregation of {product.product_code}")
                                product.completeness_score = min(
                                    len(ai_attributes) * 5, 100)
                                product.sources_consulted = golden.get(
                                    'sources_consulted', [])
                                await check_data_quality(db_session, product.product_code, ai_attributes)
                                successful += 1
                                logger.info(
                                    f"Aggregated {product.product_code}: {len(ai_attributes)} attributes")
                            # else:
                            #     found_image = aggregation_result.get('image_url')
                            #     confidence = aggregation_result.get(
                            #         'golden_record', {}).get('confidence', 0.5)
                            #     enriched_attributes = {}
                            #     for key, val in ai_attributes.items():
                            #         enriched_attributes[key] = {
                            #             "standard_value": val,
                            #             "source": "AI_Aggregation_Engine",
                            #             "timestamp": datetime.utcnow().isoformat()
                            #         }
                            #     # product.attributes = {**product.attributes, **ai_attributes}
                            #     if product.dynamic_attributes:
                            #         for attr in product.dynamic_attributes:
                            #             if isinstance(attr, dict) and attr.get('name'):
                            #                 attr_name = attr['name']
                            #                 if attr_name in ai_attributes:
                            #                     ai_val = ai_attributes[attr_name]
                            #                     if isinstance(ai_val, dict):
                            #                         attr['value'] = ai_val.get('value', '') or attr.get('value', '')
                            #                         attr['uom'] = ai_val.get('unit', '') or ai_val.get('uom', '') or attr.get('uom', '')
                            #                     else:
                            #                         attr['value'] = str(ai_val) if ai_val else attr.get('value', '')
                            #         flag_modified(product, "dynamic_attributes")
                            #     product.attributes = {**product.attributes, **ai_attributes}
                            #     if found_image and isinstance(found_image, str):
                            #         found_image_str = found_image.strip()
                            #         if found_image_str:
                            #             # Validate before saving
                            #             if await validate_image_url(found_image_str):
                            #                 product.image_url_1 = found_image_str
                            #                 logger.info(f"✓ Valid image found and saved for {product.product_code}")
                            #             else:
                            #                 logger.warning(f"⚠ Image URL invalid for {product.product_code}, not saving")
                            #         else:
                            #             logger.debug(f"Empty image URL for {product.product_code}")
                            #     else:
                            #         logger.warning(f"⚠ No image found during aggregation of {product.product_code}")
                            #     product.enrichment_status = 'completed'
                            #     product.completeness_score = min(
                            #         len(ai_attributes) * 5, 100)
                            #     product.sources_consulted = golden.get(
                            #         'sources_consulted', [])
                            #     logger.info(
                            #         f"Saving product {product.product_code} with sources: {product.sources_consulted}")
                            #     db_session.add(product)
                            #     await check_data_quality(db_session, product.product_code, ai_attributes)
                            #     successful += 1
                            #     logger.info(
                            #         f" Aggregated {product.product_code}: {len(ai_attributes)} attributes")
                            else:
                                # Standard case (no backfilling)
                                # Update product.attributes (preserve existing if new is empty)
                                for key, val in ai_attributes.items():
                                    if key not in product.attributes or (val and (not isinstance(val, dict) or val.get('value'))):
                                        product.attributes[key] = val
                                flag_modified(product, "attributes") 

                                # Merge dynamic_attributes (update existing, add new)
                                merge_dynamic_attributes(product, ai_attributes, is_validation_mode=False)

                                # Image handling
                                found_image = aggregation_result.get('image_url')
                                if found_image and isinstance(found_image, str):
                                    found_image_str = found_image.strip()
                                    if found_image_str:
                                        if await validate_image_url(found_image_str):
                                            product.image_url_1 = found_image_str
                                            logger.info(f"✓ Valid image saved for {product.product_code}")
                                        else:
                                            logger.warning(f"⚠ Image invalid for {product.product_code}")
                                    else:
                                        logger.debug(f"Empty image URL for {product.product_code}")
                                else:
                                    logger.warning(f"⚠ No image found for {product.product_code}")

                                product.enrichment_status = 'completed'
                                product.completeness_score = min(len(ai_attributes) * 5, 100)
                                product.sources_consulted = golden.get('sources_consulted', [])
                                await check_data_quality(db_session, product.product_code, ai_attributes)
                                successful += 1
                                logger.info(f" Aggregated {product.product_code}: {len(ai_attributes)} attributes")
                        else:
                            product.enrichment_status = 'failed'
                            db_session.add(product)
                            failed += 1
                            failed_products.append({
                                'sku': product.product_code,
                                'error': aggregation_result.get('reason', 'Unknown error')
                            })
                            logger.warning(f" Aggregation failed for {product.product_code}")
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
            job.completed_at = datetime.utcnow()
            job.details = {
                **job.details,
                'failed_products': failed_products[:50],
                'completed_at': datetime.utcnow().isoformat()
            }
            db_session.add(job)
            source_stmt = select(Source).where(
                Source.project_id == job.project_id)
            source_result = await db_session.execute(source_stmt)
            sources = source_result.scalars().all()
            for source in sources:
                new_metadata = dict(
                    source.source_metadata) if source.source_metadata else {}
                new_metadata['processing_status'] = 'completed'
                new_metadata['successful'] = successful
                new_metadata['failed'] = failed
                new_metadata['last_run'] = datetime.utcnow().isoformat()
                source.source_metadata = new_metadata
                flag_modified(source, "source_metadata")
                db_session.add(source)
            db_session.add(AuditTrail(
                product_id=f"PROJECT_{job.project_id}",
                stage="aggregation",
                attribute_name="project_aggregation",
                selected_value="Completed" if job.status == 'completed' else "Cancelled",
                sources_used=f"{total} products",
                reason=f"Aggregated {successful}/{total} products successfully, {failed} failed"
            ))
            await db_session.commit()
            logger.info(
                f"Job {job_id} complete: {successful}/{total} successful, {failed} failed")
        except Exception as e:
            await db_session.rollback()
            logger.error(
                f"Aggregation job {job_id} failed: {e}", exc_info=True)
            if job:
                try:
                    job.status = 'failed'
                    job.error_message = str(e)[:500]
                    job.completed_at = datetime.utcnow()
                    db_session.add(job)
                    await db_session.commit()
                except Exception as commit_error:
                    logger.error(
                        f"Failed to update job status: {commit_error}")
                    
async def run_single_product_aggregation(product_id: str,llm_provider:str='openai') -> None:
        async with async_session_factory() as db_session:
            try:
                product = await db_session.get(Product, product_id)
                if not product:
                    logger.error(f"Product {product_id} not found")
                    return
                logger.info(
                    f"Starting single product aggregation: {product.product_code}")
                primary_attrs = []
                if product.dynamic_attributes:
                    for attr in product.dynamic_attributes:
                        attr_name = attr.get('name')
                        if attr_name and str(attr_name).strip():
                            primary_attrs.append(str(attr_name).strip())
                logger.info(f"   └─ Taxonomy: {product.taxonomy}")
                logger.info(f"   └─ Primary attrs: {primary_attrs}")
                if len(primary_attrs) > 10:
                    logger.info(f"Product has {len(primary_attrs)} attributes - using multi-pass processing")
                    attr_chunks = chunk_attributes(primary_attrs, chunk_size=10)
                    merged_ai_data = {}
                    all_sources = []
                    image_url = None
                    short_desc=None
                    long_desc=None
                    features=None
                    for idx, chunk in enumerate(attr_chunks, 1):
                        logger.info(f"   └─ Pass {idx}/{len(attr_chunks)}: Processing attributes {chunk}")
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
                                logger.info(f"Image captured from pass {idx} {image_url}")
                            if not short_desc and golden.get('short_description'):
                                short_desc=golden.get('short_description')
                            if not long_desc and golden.get('long_description'):
                                long_desc=golden.get('long_description')
                            if not features and golden.get('features'):
                                features=golden.get('features')
                        await asyncio.sleep(1)
                    result = {
                        'status': 'success' if merged_ai_data else 'failed',
                        'golden_record': {
                            'attributes': merged_ai_data,
                            'sources_consulted': list(set(all_sources)),  
                            'short_description': short_desc or product.short_description,
                            'long_description':long_desc or product.long_description,
                            'features': features or product.features
                        },
                        'image_url': image_url
                    }
                    logger.info(f" Multi-pass complete: {len(merged_ai_data)} total attributes from {len(attr_chunks)} passes")
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
                    if 'back filling' in use_case.lower() or 'validation' in use_case.lower():
                        existing_attrs = {}
                        conflicts = {}
                        ai_data_for_merge = {}
                        if product.dynamic_attributes:
                            for attr in product.dynamic_attributes:
                                if isinstance(attr, dict):
                                    name = attr.get('name')
                                    existing_attrs[name] = {'value': attr.get('value'), 'uom': attr.get('uom')}
                        for attr_name, ai_val in ai_data.items():
                            if isinstance(ai_val, dict) and ai_val.get("matches_excel") is False:
                                conflicts[attr_name] = extract_ai_value_text(ai_val)
                            ai_data_for_merge[attr_name] = ai_val
                        # product.attributes = merge_attributes_preserving_order(
                        #     primary_attributes=primary_attrs,
                        #     existing_attrs=existing_attrs,
                        #     ai_data=ai_data_for_merge
                        # )
                        # product.validation_conflicts = conflicts 
                        # if "validation" in use_case and product.dynamic_attributes:
                        #     for attr in product.dynamic_attributes:
                        #         if isinstance(attr, dict) and attr.get('name'):
                        #             attr_name = attr['name']
                        #             if attr_name in ai_data_for_merge:
                        #                 ai_val = ai_data_for_merge[attr_name]
                        #                 if isinstance(ai_val, dict):
                        #                     attr['validation_value'] = ai_val.get('value', '')
                        #                     attr['validation_uom'] = ai_val.get('unit', '') or ai_val.get('uom', '')
                        #                 else:
                        #                     attr['validation_value'] = str(ai_val) if ai_val else ''
                        #     flag_modified(product, "dynamic_attributes")
                        product.attributes = merge_attributes_preserving_order(
                            primary_attributes=primary_attrs,
                            existing_attrs=existing_attrs,
                            ai_data=ai_data_for_merge
                        )
                        product.validation_conflicts = conflicts
                        flag_modified(product, "validation_conflicts")

                        # Merge dynamic_attributes (update existing, add new, set validation fields)
                        merge_dynamic_attributes(product, ai_data_for_merge, is_validation_mode=('validation' in use_case))

                        # Image handling (keep the existing image handling code that follows)
                        product.enrichment_status = 'completed'
                        await db_session.flush() 
                        db_session.add(product)
                        existing_image = product.image_url_1
                        found_image = result.get('image_url')
                        
                        # Image validation and handling
                        if found_image and isinstance(found_image, str):
                            found_image_str = found_image.strip()
                            if found_image_str:
                                # Validate the image URL
                                if await validate_image_url(found_image_str):
                                    product.image_url_1 = found_image_str
                                    logger.info(f"✓ Valid image saved for {product.product_code}")
                                else:
                                    # Image validation failed, keep existing if available
                                    if existing_image:
                                        product.image_url_1 = existing_image
                                        logger.warning(f"⚠ New image invalid, keeping existing for {product.product_code}")
                                    else:
                                        logger.warning(f"⚠ Image validation failed and no existing image for {product.product_code}")
                            else:
                                logger.debug(f"Empty image URL for {product.product_code}")
                        else:
                            logger.warning(f"⚠ No image found during single product aggregation of {product.product_code}")
                        product.completeness_score = min(len(ai_data) * 5, 100)
                        product.sources_consulted = golden.get(
                            'sources_consulted', [])
                        await check_data_quality(db_session, product.product_code, ai_data)
                        logger.info(
                            f"Single product aggregation complete: {product.product_code}")
                    # else:
                    #     product.attributes = {**product.attributes, **ai_data}
                    #     found_image = result.get('image_url')
                    #     logger.info(
                    #         f" Single product - Image found: {found_image}")
                    #     if found_image and isinstance(found_image, str) and found_image.strip():
                    #         product.image_url_1 = found_image.strip()
                    #         logger.info(f" Image URL saved: {product.image_url_1}")
                    #     else:
                    #         logger.warning(f"⚠ No image found for {product.product_code}")
                        
                    #     product.enrichment_status = 'completed'
                    #     await db_session.flush() 
                    #     product.completeness_score = min(len(ai_data) * 5, 100)
                    #     product.sources_consulted = golden.get(
                    #         'sources_consulted', [])
                    #     await check_data_quality(db_session, product.product_code, ai_data)
                    # else:
                    #     if product.dynamic_attributes:
                    #         for attr in product.dynamic_attributes:
                    #             if isinstance(attr,dict) and attr.get('name'):
                    #                 attr_name=attr['name']
                    #                 if attr_name in ai_data:
                    #                     ai_val=ai_data[attr_name]
                    #                     if isinstance(ai_val, dict):
                    #                         attr['value'] = ai_val.get('value', '') or attr.get('value', '')
                    #                         attr['uom'] = ai_val.get('unit', '') or ai_val.get('uom', '') or attr.get('uom', '')
                    #                     else:
                    #                         attr['value'] = str(ai_val) if ai_val else attr.get('value', '')
                    #         flag_modified(product, "dynamic_attributes")
                    #     product.attributes = {**product.attributes, **ai_data}
                    #     found_image = result.get('image_url')
                    #     if found_image and isinstance(found_image, str) and found_image.strip():
                    #         if await validate_image_url(found_image.strip()):
                    #             product.image_url_1 = found_image.strip()
                    #             logger.info(f"✓ Valid image saved for {product.product_code}")
                    #         else:
                    #             logger.warning(f"⚠ Image invalid for {product.product_code}")
                    #     else:
                    #         logger.warning(f"⚠ No image found for {product.product_code}")
                        
                    #     product.enrichment_status = 'completed'
                    #     await db_session.flush()
                    #     product.completeness_score = min(len(ai_data) * 5, 100)
                    #     product.sources_consulted = golden.get('sources_consulted', [])
                    #     await check_data_quality(db_session, product.product_code, ai_data)
                    else:
                        for key, val in ai_data.items():
                            if key not in product.attributes or (val and (not isinstance(val, dict) or val.get('value'))):
                                product.attributes[key] = val
                        flag_modified(product, "attributes") 
                        merge_dynamic_attributes(product, ai_data, is_validation_mode=False)

                        found_image = result.get('image_url')
                        if found_image and isinstance(found_image, str) and found_image.strip():
                            if await validate_image_url(found_image.strip()):
                                product.image_url_1 = found_image.strip()
                                logger.info(f"✓ Valid image saved for {product.product_code}")
                            else:
                                logger.warning(f"⚠ Image invalid for {product.product_code}")
                        else:
                            logger.warning(f"⚠ No image found for {product.product_code}")

                        product.enrichment_status = 'completed'
                        product.completeness_score = min(len(ai_data) * 5, 100)
                        product.sources_consulted = golden.get('sources_consulted', [])
                        await check_data_quality(db_session, product.product_code, ai_data)
                    remaining_stmt = select(func.count(Product.id)).where(
                        and_(
                            Product.project_id == product.project_id,
                            Product.enrichment_status.in_(['pending', 'processing'])
                        )
                    )
                    remaining_count = await db_session.scalar(remaining_stmt)
                    failed_stmt = select(func.count(Product.id)).where(and_(Product.project_id == product.project_id,Product.enrichment_status == 'failed'))
                    failed_count=await db_session.scalar(failed_stmt)
                    if remaining_count == 0:
                        if failed_count>0:
                            logger.info(f" Project {product.project_id} completed with {failed_count} failed products")
                        else:
                            logger.info(f" Project {product.project_id} is FULLY COMPLETED!")
                        await db_session.execute(update(Project).where(Project.id == product.project_id).values(status='completed'))
                        source_stmt = select(Source).where(
                            Source.project_id == product.project_id)
                        source_result = await db_session.execute(source_stmt)
                        sources = source_result.scalars().all()
                        for source in sources:
                            new_metadata = dict(
                                source.source_metadata) if source.source_metadata else {}
                            new_metadata['processing_status'] = 'completed'
                            new_metadata['completed_at'] = datetime.utcnow(
                            ).isoformat()
                            source.source_metadata = new_metadata
                            flag_modified(source, "source_metadata")
                            db_session.add(source)
                            logger.info(
                                f" Updated source {source.id} metadata: {new_metadata}")
                        db_session.add(AuditTrail(
                            product_id=f"PROJECT_{product.project_id}",
                            stage="aggregation",
                            attribute_name="project_completion",
                            selected_value="Completed",
                            sources_used="All products",
                            reason="All products aggregated successfully"
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
                except Exception:
                    pass
worker_pool = get_worker_pool(process_function=run_single_product_aggregation)

async def aggregate_with_retry(
    db_session,
    mpn: str,
    title: str,
    sku:str,
    upc:Optional[str]=None,
    brand: Optional[str] = None,
    taxonomy: Optional[str] = None,
    primary_attributes: Optional[List[str]] = None,
    attribute_chunk:Optional[List[str]]=None,
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
@router.delete("/jobs/cleanup")
async def cleanup_old_aggregation_jobs(
    days: int = 7,
    db: AsyncSession = Depends(get_session)
) -> Dict[str, Any]:
    try:
        count = await cleanup_old_jobs(db, days)
        return {
            'status': 'success',
            'message': f'Cleaned up {count} old jobs',
            'deleted_count': count
        }
    except Exception as e:
        logger.error(f"Failed to cleanup jobs: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to cleanup old jobs"
        )
async def export_project_data(project_id: str, db: AsyncSession = Depends(get_session)):
    try:
        stmt = select(Product).where(Product.project_id == project_id)
        result = await db.execute(stmt)
        products = result.scalars().all()
        if not products:
            raise HTTPException(
                status_code=404, detail="No products found in this project")
        export_data = []
        for p in products:
            row = {
                "Product ID": str(p.id),
                "MPN": p.product_code,
                "Name": p.product_name,
                "Brand": p.brand_name,
                "Status": p.enrichment_status,
                "Completeness": f"{p.completeness_score}%"
            }
            if p.attributes:
                for key, val in p.attributes.items():
                    clean_key = key.replace('_', ' ').title()
                    row[clean_key] = str(val)
            export_data.append(row)
        df = pd.DataFrame(export_data)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Aggregated Data')
        output.seek(0)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        filename = f"Project_Export_{timestamp}.xlsx"
        return StreamingResponse(output, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers={
            "Content-Disposition": f"attachment; filename={filename}"
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Export failed for project {project_id}: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to generate export file")

@router.post('/export/batch',status_code=200)
async def batch_export_products(request:BatchExportRequest,db:AsyncSession=Depends(get_session)):
    try:
        
        product_id_set=set(request.product_ids)
        if request.project_ids:
            stmt=select(Product.id).where(Product.project_id.in_(request.project_ids))
            result=await db.execute(stmt)
            product_ids_from_projects=result.scalars().all()
            product_id_set.update(product_ids_from_projects)
        if not product_id_set:
            raise HTTPException(status_code=400,detail='No products selected')
        stmt=select(Product).where(Product.id.in_(list(product_id_set)))
        result=await db.execute(stmt)
        products=result.scalars().all()
        if not products:
            raise HTTPException(status_code=404,detail='No products found')
        return await generate_products_excel(products,db)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'Batch export failed {e}')
        raise HTTPException(status_code=500,detail='Failed to download results!')