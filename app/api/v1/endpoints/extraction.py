from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status, Form, UploadFile, File, Request, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from sqlalchemy import func
from app.models.pipeline import AuditTrail, RawExtraction, Source, SourcePriority
from app.core.database import get_session, async_session_factory
from app.models.product import Product
from typing import List, Optional
import logging
from sqlalchemy.orm.attributes import flag_modified
from urllib.parse import quote
from app.utils.aggregate_download import generate_products_excel
from app.utils.usecase_validator import validate_file_against_use_case
import json
import io
import hashlib
import asyncio
import re
from datetime import datetime, timedelta
from app.aggregation.aggregate_product import aggregate_product
from app.schemas.extraction import ExtractionRequest, SourceMetricsResponse, SourceResponse
from app.schemas.pipeline import SourcePriorityResponse
from app.aggregation.prompt_builder import get_taxonomy_attribute_hints
import pandas as pd
import os
from app.models.project import Project
from uuid import uuid4
from app.utils.parsers import infer_taxonomy_for_row, parse_import_file
from app.utils.matching import get_or_create_brand, get_or_create_vendor, get_or_create_industry
from app.utils.sanitize import sanitize_ai_data
from app.api.v1.endpoints.aggregation import extract_ai_value_text
logger = logging.getLogger("extraction_router")
router = APIRouter()
ALLOWED_EXTENSIONS = {'.csv', '.xlsx', '.xls'}
MAX_FILE_SIZE = 10 * 1024 * 1024
MAX_ROWS = 1000
def merge_attributes_preserving_order(
    primary_attributes: List[str],
    existing_attrs: dict,
    ai_data: dict
) -> dict:
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
@router.get("/", response_model=List[SourceResponse])
async def getAllSources(db: AsyncSession = Depends(get_session)):
    try:
        statement = select(Source).order_by(Source.uploaded_at.desc())
        result = await db.execute(statement)
        sources = result.scalars().all()
        return sources
    except Exception as e:
        logger.error(f"Failed to fetch sources: {str(e)}")
        raise HTTPException(
            status_code=500, detail="Could not retrieve import history")
@router.post("/", status_code=status.HTTP_202_ACCEPTED)
async def extract_from_source(
    payload: ExtractionRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session)
):
    try:
        if not payload.projectId:
            logger.error(f'No project ID in manual extraction')
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Project ID is required,Please select a project first!")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        formatted_name = f"Manual_{timestamp}_{payload.sourceUrl}"
        new_source = Source(
            source_type=payload.sourceType,
            source_url=formatted_name,
            project_id=payload.projectId,
            status="processing",
            content_data=payload.content.encode('utf-8'),
            source_metadata={"raw_length": len(payload.content)}
        )
        db.add(new_source)
        await db.commit()
        await db.refresh(new_source)
        background_tasks.add_task(
            run_extraction_task,
            str(new_source.id),
            payload.content,
        )
        return {
            "status": "accepted",
            "source_id": str(new_source.id),
            "message": "AI pipeline initialized in background"
        }
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to initialize extraction: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="System failed to initialize the extraction pipeline"
        )
@router.get("/priorities/{project_id}", response_model=List[SourcePriorityResponse], status_code=status.HTTP_200_OK)
async def get_project_priorities(project_id: str, db: AsyncSession = Depends(get_session)):
    try:
        statement = (
            select(SourcePriority)
            .where(SourcePriority.project_id == project_id)
            .order_by(SourcePriority.priority_rank.asc())
        )
        result = await db.execute(statement)
        priorities = result.scalars().all()
        logger.info(
            f"Retrieved {len(priorities)} priority rankings for project {project_id}")
        return priorities
    except Exception as e:
        logger.error(f"DATABASE ERROR in get_project_priorities: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal system error while retrieving source rankings"
        )
@router.get('/project/{project_id}', response_model=List[SourceResponse])
async def get_sources_by_project(project_id: str, db: AsyncSession = Depends(get_session)):
    try:
        if project_id is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Project id is required")
        statement = select(Source).where(Source.project_id ==
                                         project_id).order_by(Source.uploaded_at.desc())
        result = await db.execute(statement)
        sources = result.scalars().all()
        return sources
    except Exception as e:
        logger.error(f"Failed to fetch project sources:{e}")
        return []
def sanitize_for_excel(val):
    if not isinstance(val, str):
        return val
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', val)
def clean_for_excel(val, attr_name=None):
    if val is None or val == "":
        return ""
    if isinstance(val, dict):
        if "standard_value" in val:
            return clean_for_excel(val["standard_value"], attr_name)
        if "value" in val:
            return clean_for_excel(val["value"], attr_name)
        if attr_name:
            target = str(attr_name).lower().replace("_", "").replace(" ", "")
            for k, v in val.items():
                if target in k.lower().replace("_", ""):
                    return clean_for_excel(v, attr_name)
        vals = [str(clean_for_excel(v, attr_name))
                for v in val.values() if v is not None and v != ""]
        return ", ".join([v for v in vals if v])
    if isinstance(val, list):
        cleaned_list = [str(clean_for_excel(i, attr_name))
                        for i in val if i is not None and i != ""]
        return " | ".join(i for i in cleaned_list if i)
    val_str = str(val).strip()
    if val_str.lower() in ["n/a", "none", "null", "nan", "not available", "increase", "*"]:
        return ""
    return sanitize_for_excel(val_str)
@router.get("/{source_id}/download")
async def download_file(
    source_id: str,
    download_type: str = Query("input", alias="type"),
    db: AsyncSession = Depends(get_session)
):
    try:
        source = await db.get(Source, source_id)
        if not source:
            raise HTTPException(
                status_code=404, detail="Source record not found")
        if download_type == 'input':
            if source.content_data:
                filename = f"Input_{source.source_url}"
                encoded_filename = quote(filename)
                if source.source_url.lower().endswith('.pdf'):
                    media_type = 'application/pdf'
                else:
                    media_type = "application/octet-stream"
                return StreamingResponse(
                    io.BytesIO(source.content_data),
                    media_type=media_type,
                    headers={
                        "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
                        "Content-Length": str(len(source.content_data))
                    }
                )
            else:
                mpns = source.source_metadata.get('mpns', [])
                if not mpns:
                    mpn = source.source_metadata.get('mpn')
                    mpns = [mpn] if mpn else []
                if mpns:
                    content = f"MPNs submitted for extraction:\n\n" + "\n".join(mpns)
                    content += f"\n\nTotal: {len(mpns)} MPN(s)"
                    content += f"\nUse Case: {source.source_metadata.get('use_case', 'N/A')}"
                    return StreamingResponse(
                        io.BytesIO(content.encode('utf-8')),
                        media_type="text/plain",
                        headers={
                            "Content-Disposition": f"attachment; filename=mpns_{str(source.id)[:8]}.txt",
                            "Content-Length": str(len(content))
                        }
                    )
                else:
                    return StreamingResponse(
                        io.BytesIO(b"No data available"), 
                        media_type="text/plain"
                    )
        elif download_type == 'output':
            stmt = select(Product).where(
                Product.project_id == source.project_id,
                Product.source_url == source.source_url
            ).order_by(Product.created_at.asc())
            result = await db.execute(stmt)
            products = result.scalars().all()
            if not products:
                mpns = source.source_metadata.get('mpns', [])
                if not mpns:
                    mpn = source.source_metadata.get('mpn')
                    mpns = [mpn] if mpn else []
                if mpns:
                    stmt = select(Product).where(
                        Product.project_id == source.project_id,
                        Product.product_code.in_(mpns)
                    ).order_by(Product.created_at.asc())
                    result = await db.execute(stmt)
                    products = result.scalars().all()
            if not products:
                raise HTTPException(
                    status_code=404, detail="No enriched data found")
            project = await db.get(Project, products[0].project_id) if products else None
            use_case_lower = (project.use_case or "").lower() if project else ""
            if 'back filling' in use_case_lower or 'validation' in use_case_lower:
                MAX_ATTRIBUTES = 40
            else:
                MAX_ATTRIBUTES = 40
            logger.info(
                f"Using {MAX_ATTRIBUTES} attribute columns for use case: {project.use_case if project else 'Unknown'}")
            project_name = project.name if project else None
            return await generate_products_excel(products, db, global_project_name=project_name)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Download Error: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Error generating download")
@router.post("/batch-aggregate", status_code=status.HTTP_202_ACCEPTED)
async def batch_aggregate(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    projectId: str = Form(...),
    db: AsyncSession = Depends(get_session)
):
    try:
        if not projectId or not projectId.strip():
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "Project ID is required.")
        project = await db.get(Project, projectId)
        if not project:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"Project {projectId} not found")
        file_ext = os.path.splitext(file.filename)[1].lower()
        if file_ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "Invalid file type.")
        content = bytearray()
        chunk_size = 1024 * 1024
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > MAX_FILE_SIZE:
                raise HTTPException(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File too large")
        content = bytes(content)
        file_hash = hashlib.sha256(content).hexdigest()
        recent_cutoff = datetime.utcnow() - timedelta(hours=24)
        duplicate_check = select(Source).where(
            Source.project_id == projectId,
            func.json_extract_path_text(
                Source.source_metadata, 'file_hash') == file_hash,
            Source.created_at > recent_cutoff
        )
        if await db.scalar(duplicate_check):
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "File already uploaded recently.")
        rows = parse_import_file(content, file.filename)
        valid_rows = []
        rejected_count = 0
        with_mpn_count=0
        without_mpn_count=0
        for r in rows:
            mpn = str(r.get('mpn', '')).strip()
            brand = str(r.get('brand', '')).strip()
            sku=str(r.get('sku','')).strip()
            product_name=str(r.get('product_name',"")).strip()
            has_identifier=bool(mpn or sku or product_name)
            if brand and has_identifier:
                valid_rows.append(r)
                if mpn:
                    with_mpn_count+=1
                else:
                    without_mpn_count+=1
            else:
                rejected_count += 1
                logger.warning(f"Rejected row: missing required fields Brand='{brand}', MPN='{mpn}', SKU='{sku}', Product_Name='{product_name}'")
        if rejected_count > 0:
            logger.info(
                f"Rejected {rejected_count} rows due to missing SKU, MPN, or Brand")
        if not valid_rows:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,detail="No valid rows found. Each row must contain Brand and at least one of MPN, SKU, or Product Name.")
        rows = valid_rows
        total_rows = len(rows)
        if total_rows == 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "File is empty or invalid format")
        if total_rows > MAX_ROWS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"Too many rows ({total_rows}). Max {MAX_ROWS}.")
        logger.info(
            f"Validating {total_rows} products against use case: {project.use_case}")
        validation_result = validate_file_against_use_case(
            rows, project.use_case)
        if not validation_result['valid']:
            logger.error(f"Validation failed")
            logger.error(f"{validation_result['error']}")
            raise HTTPException(status_code=400, detail={'type': "validation_error", 'message': "File does not match project requirements",
                                'error': validation_result['error'], 'requirements': validation_result.get('requirements', []), 'use_case': project.use_case, 'project_name': project.name})
        logger.info(f"Validation passed")
        logger.info(
            f"   {validation_result.get('message', 'File is compatible')}")
        inferred_count = 0
        for row in rows:
            if not row.get("taxonomy"):
                inferred = infer_taxonomy_for_row(row, rows)
                if inferred:
                    row["taxonomy"] = inferred
                    inferred_count += 1
        new_source = Source(
            source_type="excel" if file_ext in ['.xlsx', '.xls'] else "csv",
            source_url=f"Import_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            project_id=projectId,
            status="completed",
            uploaded_at=datetime.utcnow(),
            content_data=content,
            source_metadata={
                "file_hash": file_hash,
                "total": total_rows,
                "inferred_taxonomies": inferred_count,
                "processing_status": "pending",
                'with_mpn_count':with_mpn_count,
                'without_mpn_count':without_mpn_count,
                'rejected_count':rejected_count
            }
        )
        db.add(new_source)
        await db.commit()
        await db.refresh(new_source)
        created_count = 0
        updated_count = 0
        for idx, row in enumerate(rows):
            code = row.get("mpn") or row.get("sku") or f"UNK-{uuid4()}"
            stmt = select(Product).where(Product.product_code == str(code),Product.project_id==projectId)
            result = await db.execute(stmt)
            product = result.scalars().first()
            if not product:
                product = Product(
                    product_code=str(code),
                    project_id=projectId,
                    created_at=datetime.utcnow()
                )
                created_count += 1
            else:
                product.project_id = projectId
                updated_count += 1
            product.product_name = row.get("product_name", "Unknown")
            product.mpn = row.get("mpn")
            product.sku = row.get("sku")
            product.taxonomy = row.get("taxonomy")
            product.source_url = new_source.source_url
            if row.get('dynamic_attributes'):
                product.dynamic_attributes = row['dynamic_attributes']
            product.category_1 = row.get('category_1')
            product.category_2 = row.get('category_2')
            product.category_3 = row.get('category_3')
            product.category_4 = row.get('category_4')
            product.category_5 = row.get('category_5')
            product.category_6 = row.get('category_6')
            product.category_7 = row.get('category_7')
            product.category_8 = row.get('category_8')
            product.gtin = row.get('gtin')
            product.ean = row.get('ean')
            product.upc = row.get('upc')
            product.unspc = row.get('unspc')
            product.product_type = row.get('product_type')
            product.parent_sku = row.get('parent_sku')
            product.lifecycle_stage = row.get('lifecycle_stage')
            product.launch_date = row.get('launch_date')
            product.discontinue_status = row.get('discontinue_status')
            try:
                if row.get('weight'):
                    product.weight = str(row['weight']).replace(',', '')
                product.weight_unit = row.get('weight_unit')
                if row.get('length'):
                    product.length = str(row['length']).replace(',', '')
                if row.get('width'):
                    product.width = str(row['width']).replace(',', '')
                if row.get('height'):
                    product.height = str(row['height']).replace(',', '')
                product.dimension_unit = row.get('dimension_unit')
            except ValueError:
                pass
            brand = await get_or_create_brand(db, row.get("brand"))
            if brand:
                product.brand_id = brand.id
                product.brand_name = brand.name
                if row.get('country_of_origin'):
                    brand.country_of_origin = row.get('country_of_origin')
                    db.add(brand)
            vendor = await get_or_create_vendor(db, row.get("vendor"))
            if vendor:
                product.vendor_id = vendor.id
                product.vendor_name = vendor.name
            industry = await get_or_create_industry(db, row.get("industry_name"))
            if industry:
                product.industry_id = industry.id
                product.industry_name = industry.name
            try:
                if row.get("base_price"):
                    product.base_price = float(
                        str(row["base_price"]).replace(',', ''))
                    product.currency = row.get("currency", "USD")
            except ValueError:
                pass
            product.enrichment_status = "pending"
            db.add(product)
        await db.commit()
        logger.info(
            f"Batch import saved: {created_count} new, {updated_count} updated. Waiting for manual aggregation.")
        return {
            "status": "accepted",
            "batch_id": str(new_source.id),
            "message": f"Imported {len(valid_rows)} products. Ready for aggregation.",
             "summary": {
            "total_rows": total_rows,
            "valid_rows": len(valid_rows),
            "rejected_rows": rejected_count,
            "with_mpn_count": with_mpn_count,
            "without_mpn_count": without_mpn_count,
    }
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Batch processing error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Batch processing failed: {str(e)}"
        )
@router.get("/batch-status/{batch_id}")
async def get_batch_status(batch_id: str, db: AsyncSession = Depends(get_session)):
    try:
        source = await db.get(Source, batch_id)
        if not source:
            raise HTTPException(status_code=404, detail="Batch not found")
        return {
            "batch_id": str(source.id),
            "status": source.status,
            "metadata": source.source_metadata,
            "created_at": source.uploaded_at
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching batch status: {str(e)}")
        raise HTTPException(
            status_code=500, detail="Failed to fetch batch status")
@router.get("/{source_id}/metrics", response_model=SourceMetricsResponse)
async def get_source_metrics(source_id: str, db: AsyncSession = Depends(get_session)):
    try:
        source = await db.get(Source, source_id)
        if not source:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Source record not found"
            )
        statement = select(RawExtraction).where(
            RawExtraction.source_id == source_id)
        result = await db.execute(statement)
        extractions = result.scalars().all()
        if not extractions:
            return {
                "avgConfidence": 0.0,
                "completeness": 0.0,
                "totalAttributes": 0
            }
        total_conf = sum(ext.confidence for ext in extractions)
        avg_conf = total_conf / len(extractions)
        unique_attributes = set()
        for ext in extractions:
            if isinstance(ext.raw_attributes, dict):
                unique_attributes.update(ext.raw_attributes.keys())
        total_attrs_count = len(unique_attributes)
        TARGET_ATTR_COUNT = 20
        completeness_score = min(total_attrs_count / TARGET_ATTR_COUNT, 1.0)
        logger.info(
            f"Metrics generated for source {source_id}: {total_attrs_count} attrs found")
        return {
            "avgConfidence": round(avg_conf, 2),
            "completeness": round(completeness_score, 2),
            "totalAttributes": total_attrs_count
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"CRITICAL ERROR calculating metrics for {source_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Analytics engine failed to calculate metrics"
        )
async def run_extraction_task(source_id: str, content: str):
    async with async_session_factory() as db_session:
        try:
            source = await db_session.get(Source, source_id)
            if not source:
                return
            if not source.project_id:
                logger.error(
                    f"Source {source_id} has NO project_id! Aborting.")
                return
            items_to_process = []
            try:
                items_to_process = json.loads(content)
            except:
                lines = content.split('\n')
                current_manual_item = {}
                for line in lines:
                    if ':' in line:
                        k, v = line.split(':', 1)
                        current_manual_item[k.strip().lower()] = v.strip()
                if current_manual_item:
                    items_to_process.append(current_manual_item)
            successful = 0
            failed = 0
            for item in items_to_process:
                try:
                    sku = (item.get('mpn') or item.get('sku') or item.get(
                        'product_code') or "").strip().upper()
                    title = item.get('title') or item.get(
                        'product_name') or item.get('name') or ""
                    brand = item.get('brand') or item.get('brand_name') or ""
                    if not sku:
                        failed += 1
                        continue
                    stmt = select(Product).where(Product.product_code == sku)
                    prod_result = await db_session.execute(stmt)
                    product = prod_result.scalars().first()
                    raw_attributes = {k: v for k, v in item.items() if v and k not in [
                        'mpn', 'sku', 'product_code']}
                    if not product:
                        product = Product(
                            product_code=sku,
                            product_name=title or sku,
                            brand_name=brand,
                            mpn=sku,
                            project_id=source.project_id,
                            source_url=source.source_url,
                            attributes=raw_attributes,
                            enrichment_status='pending',
                            completeness_score=10
                        )
                        db_session.add(product)
                        logger.info(f"Created product: {sku}")
                    else:
                        product.source_url = source.source_url
                        product.attributes = {
                            **product.attributes, **raw_attributes}
                        product.enrichment_status = 'pending'
                        db_session.add(product)
                        logger.info(f"Updated product: {sku}")
                    db_session.add(RawExtraction(
                        source_id=source.id,
                        product_keys={"sku": sku, "mpn": sku},
                        raw_attributes=raw_attributes,
                        confidence=0.0,
                        extracted_at=datetime.utcnow()
                    ))
                    successful += 1
                except Exception as e:
                    logger.error(f"Failed to process item: {e}")
                    failed += 1
                    continue
            source.status = "completed"
            source.source_metadata = {
                "total": len(items_to_process),
                "successful": successful,
                "failed": failed,
                "processing_status": "pending"
            }
            db_session.add(source)
            db_session.add(AuditTrail(
                product_id="BATCH_UPLOAD",
                stage="extraction",
                attribute_name="ingestion",
                selected_value="Success",
                sources_used=source.source_url,
                reason=f"Imported {successful} products (awaiting aggregation)"
            ))
            await db_session.commit()
            logger.info(
                f"✓ Import complete for {source_id}: {successful} products stored")
        except Exception as e:
            await db_session.rollback()
            logger.error(f"Import failed for {source_id}: {str(e)}")
            try:
                async with async_session_factory() as error_session:
                    s = await error_session.get(Source, source_id)
                    if s:
                        s.status = "failed"
                        s.source_metadata = {"error": str(e)}
                        error_session.add(s)
                        await error_session.commit()
            except:
                pass
@router.post('/aggregate/{source_id}', status_code=status.HTTP_202_ACCEPTED)
async def trigger_aggregation(source_id: str, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_session)):
    try:
        source = await db.get(Source, source_id)
        if not source:
            raise HTTPException(status_code=404, detail='Source not found')
        if source.status != "completed":
            raise HTTPException(
                status_code=400, detail="Source must be imported first")
        source.source_metadata = {
            **source.source_metadata,
            'processing_status': 'processing'
        }
        db.add(source)
        await db.commit()
        background_tasks.add_task(run_aggregation_task, str(source.id))
        return {
            'status': 'accepted',
            'message': "Aggregation started in the background",
            'source_id': source.id
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to trigger aggregation:{str(e)}")
        raise HTTPException(
            status_code=500, detail="Failed to start aggregation")
def normalize_key(key: str) -> str:
    return str(key).lower().replace("_", "").replace(" ", "").strip()
def fuzzy_match_key(key: str, key_list: List[str]) -> Optional[str]:
    def normalize(s):
        s = re.sub(r'\(.*?\)', '', s)
        return s.lower().replace("_", "").replace(" ", "").strip()
    target = normalize(key)
    for k in key_list:
        if normalize(k) == target:
            return k
    return None
async def run_aggregation_task(source_id: str):
    async with async_session_factory() as db_session:
        try:
            source = await db_session.get(Source, source_id)
            if not source:
                return
            project = await db_session.get(Project, source.project_id)
            if not project or not project.use_case:
                logger.error(
                    f"Project or use_case not found for source {source_id}")
                return
            logger.info(f"Project use case {project.use_case}")
            stmt = select(Product).where(
                Product.project_id == source.project_id,
                Product.enrichment_status == 'pending'
            )
            result = await db_session.execute(stmt)
            products = result.scalars().all()
            successful = 0
            failed = 0
            total = len(products)
            products_routed_to_enrichment = 0
            products_ready_for_export=0
            logger.info(
                f"Starting aggregation task for source {source_id}, found {total} pending products.")
            for product in products:
                logger.info(
                    f" DB CHECK [{product.product_code}]: Taxonomy='{product.taxonomy}', Attrs={product.dynamic_attributes}")
                primary_attr_names = []
                if product.dynamic_attributes:
                    primary_attr_names = [
                        attr['name'] for attr in product.dynamic_attributes
                        if isinstance(attr, dict) and attr.get('name')
                    ]
                logger.info(f"PRIORITY LIST: {primary_attr_names}")
            for idx, product in enumerate(products):
                try:
                    logger.info(
                        f"Aggregating {idx+1}/{total}: {product.product_code}")
                    primary_attr_names = []
                    if product.dynamic_attributes:
                        primary_attr_names = [
                            attr['name'] for attr in product.dynamic_attributes
                            if isinstance(attr, dict) and attr.get('name')]
                    existing_data = {}
                    if product.dynamic_attributes:
                        for attr in product.dynamic_attributes:
                            if isinstance(attr, dict) and attr.get('name'):
                                existing_data[attr['name']] = {
                                    'value': attr.get('value'),
                                    'uom': attr.get('uom') or attr.get('unit')
                                }
                    logger.info(
                        f" EXISTING DATA BUILT for {product.product_code}:")
                    logger.info(
                        f"   dynamic_attributes count: {len(product.dynamic_attributes) if product.dynamic_attributes else 0}")
                    logger.info(
                        f"   existing_data keys: {list(existing_data.keys())}")
                    logger.info(
                        f"   existing_data sample: {dict(list(existing_data.items())[:2])}")
                    for k, v in existing_data.items():
                        logger.info(
                            f"  {k}: value='{v.get('value')}', uom='{v.get('uom')}'")
                    if existing_data:
                        logger.info(
                            f" Excel attributes: {list(existing_data.keys())[:5]}")
                    logger.info(
                        f"Primary attributes found in DB: {primary_attr_names}")
                    if len(primary_attr_names) > 10:
                        logger.info(
                            f" Product has {len(primary_attr_names)} attributes - using multi-pass processing")
                        from app.aggregation.aggregate_product import chunk_attributes
                        attr_chunks = chunk_attributes(
                            primary_attr_names, chunk_size=10)
                        merged_ai_data = {}
                        all_sources = []
                        for idx, chunk in enumerate(attr_chunks, 1):
                            logger.info(
                                f"   └─ Pass {idx}/{len(attr_chunks)}: Processing attributes {chunk}")
                            chunk_result = await aggregate_product(
                                mpn=product.product_code,
                                sku=product.sku,
                                upc=product.upc,
                                title=product.product_name,
                                brand=product.brand_name,
                                taxonomy=product.taxonomy,
                                primary_attributes=primary_attr_names,
                                attribute_chunk=chunk,
                                db=db_session,
                                project_id=source.project_id,
                            )
                            if chunk_result.get('status') == 'success':
                                golden = chunk_result.get('golden_record', {})
                                chunk_attrs = golden.get('attributes', {})
                                merged_ai_data.update(chunk_attrs)
                                sources = golden.get('sources_consulted', [])
                                all_sources.extend(sources)
                            await asyncio.sleep(1)
                        aggregation_result = {
                            'status': 'success' if merged_ai_data else 'failed',
                            'golden_record': {
                                'attributes': merged_ai_data,
                                'sources_consulted': list(set(all_sources)),
                                'short_description': '',
                                'long_description': '',
                                'features': []
                            }
                        }
                        logger.info(
                            f" Multi-pass complete: {len(merged_ai_data)} total attributes")
                    else:
                        aggregation_result = await aggregate_product(
                            mpn=product.product_code,
                            sku=product.sku,
                            upc=product.upc,
                            title=product.product_name,
                            brand=product.brand_name,
                            taxonomy=product.taxonomy,
                            primary_attributes=primary_attr_names,
                            db=db_session,
                            project_id=source.project_id,
                        )
                    if aggregation_result.get('status') == 'success':
                        golden = aggregation_result.get('golden_record', {})
                        ai_attributes = golden.get('attributes', {})
                        product.enrichment_status = 'completed'
                        product.short_description = sanitize_ai_data(
                            golden.get('short_description')) or product.short_description
                        product.long_description = sanitize_ai_data(
                            golden.get('long_description')) or product.long_description
                        product.features = sanitize_ai_data(
                            golden.get('features')) or product.features
                        use_case = project.use_case.lower() if project and project.use_case else ""
                        if "back filling" in use_case or "validation" in use_case:
                            conflicts = {}
                            ai_data_for_merge = {}
                            for ai_key, ai_val in ai_attributes.items():
                                matched_primary_key = fuzzy_match_key(
                                    ai_key, primary_attr_names)
                                if matched_primary_key:
                                    if isinstance(ai_val, dict) and ai_val.get("matches_excel") is False:
                                        correction_text = extract_ai_value_text(
                                            ai_val)
                                        conflicts[matched_primary_key] = correction_text
                                        logger.info(
                                            f" Correction found for {matched_primary_key}: {correction_text}")
                                        if matched_primary_key in existing_data:
                                            del existing_data[matched_primary_key]
                                    ai_data_for_merge[matched_primary_key] = ai_val.get(
                                        "web_value") if isinstance(ai_val, dict) else ai_val
                                else:
                                    ai_data_for_merge[ai_key] = ai_val
                            if product.dynamic_attributes and "validation" in use_case:
                                for attr in product.dynamic_attributes:
                                    if isinstance(attr, dict) and attr.get('name'):
                                        attr_name = attr['name']
                                        if attr_name in ai_data_for_merge:
                                            ai_val = ai_data_for_merge[attr_name]
                                            if isinstance(ai_val, dict):
                                                attr['validation_value'] = ai_val.get(
                                                    'value', '')
                                                attr['validation_uom'] = ai_val.get(
                                                    'unit', '') or ai_val.get('uom', '')
                                            else:
                                                attr['validation_value'] = str(
                                                    ai_val) if ai_val else ''
                            product.attributes = merge_attributes_preserving_order(
                                primary_attributes=primary_attr_names,
                                existing_attrs=existing_data,
                                ai_data=ai_data_for_merge
                            )
                            product.validation_conflicts = conflicts
                            flag_modified(product, "validation_conflicts")
                            logger.info(
                                f"BACKFILL: Saved {len(conflicts)} corrections for {product.product_code}")
                        else:
                            product.attributes = {
                                **product.attributes, **sanitize_ai_data(ai_attributes)}
                        sources = golden.get('sources_consulted', [])
                        product.sources_consulted = sources
                        product.completeness_score = min(
                            len(ai_attributes) * 5, 100)
                        db_session.add(RawExtraction(
                            source_id=source.id,
                            product_keys={
                                "sku": product.product_code, "mpn": product.mpn},
                            raw_attributes=ai_attributes,
                            confidence=aggregation_result.get(
                                'golden_record', {}).get('confidence', 0.9),
                            extracted_at=datetime.utcnow()
                        ))
                        flag_modified(product, "sources_consulted")
                        db_session.add(product)
                        await db_session.commit()
                        await db_session.get(Product, product.id)
                        successful += 1
                    else:
                        failed += 1
                        product.enrichment_status = 'failed'
                        db_session.add(product)
                except Exception as e:
                    logger.error(
                        f"Aggregation loop error for {product.product_code}: {e}")
                    failed += 1
                    continue
            meta = dict(source.source_metadata or {})
            meta.update({
                "processing_status": "completed",
                "aggregated_successful": successful,
                "aggregated_failed": failed,
                "last_aggregation_time": datetime.utcnow().isoformat()
            })
            source.source_metadata = meta
            db_session.add(source)
            await db_session.commit()
            logger.info(
                f"Aggregation complete: {successful}/{total} successful, {failed} failed")
        except Exception as e:
            await db_session.rollback()
            logger.error(f"Aggregation task failed for {source_id}: {str(e)}")
