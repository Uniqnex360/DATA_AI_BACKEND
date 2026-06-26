from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status, Form, UploadFile, File, Request, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from sqlalchemy import func
from io import BytesIO
from app.auth.dependencies import get_current_user
from app.models.attribute import Attribute, AttributeValue
from app.models.pipeline import AuditTrail, RawExtraction, Source, SourcePriority
from app.core.database import get_session, async_session_factory
from app.models.product import Product
from typing import List, Optional
import logging
from sqlalchemy.orm.attributes import flag_modified
from urllib.parse import quote
from app.models.product_attribute_link import ProductAttributeLinkModel, ProductAttributeValueLinkModel
from app.models.project_product_link import ProjectProductLink
from app.models.user import User
from app.utils.aggregate_download import generate_products_excel
from app.utils.attribute_helper import ensure_category_from_path, get_category_expected_attributes, save_attributes_normalized
from app.utils.usecase_validator import validate_file_against_use_case
import json
import io
import hashlib
from app.utils.timezone import now_ist
import asyncio
import re
from datetime import datetime, timedelta, timezone
from app.aggregation.aggregate_product import aggregate_product
from app.schemas.extraction import ExtractionRequest, SourceMetricsResponse, SourceResponse
from app.schemas.pipeline import SourcePriorityResponse
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
        timestamp = now_ist().strftime("%Y%m%d_%H%M%S")
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
    format: str = Query("pdf", alias="format"),
    db: AsyncSession = Depends(get_session)
):
    try:
        source = await db.get(Source, source_id)
        if not source:
            raise HTTPException(
                status_code=404, detail="Source record not found")
        if download_type == 'input':
            if source.content_data:
                if source.source_type == "pdf_multi_pending":
                    try:
                        import pickle
                        pdf_documents = pickle.loads(source.content_data)
                        if len(pdf_documents) == 1:
                            pdf_doc = pdf_documents[0]
                            filename = pdf_doc['filename']
                            encoded_filename = quote(filename)
                            return StreamingResponse(
                                BytesIO(pdf_doc['content']),
                                media_type='application/pdf',
                                headers={
                                    "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
                                    "Content-Length": str(len(pdf_doc['content']))
                                }
                            )
                        else:
                            import zipfile
                            zip_buffer = BytesIO()
                            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                                for pdf_doc in pdf_documents:
                                    zip_file.writestr(
                                        pdf_doc['filename'], pdf_doc['content'])
                            zip_buffer.seek(0)

                            zip_filename = f"multi_pdf_batch_{str(source.id)[:8]}.zip"
                            encoded_filename = quote(zip_filename)
                            return StreamingResponse(
                                zip_buffer,
                                media_type='application/zip',
                                headers={
                                    "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
                                    "Content-Length": str(zip_buffer.getbuffer().nbytes)
                                }
                            )
                    except Exception as e:
                        raise e
                filename = source.source_url
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
                    content = f"MPNs submitted for extraction:\n\n" + \
                        "\n".join(mpns)
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
            stmt = select(Product).join(ProjectProductLink).where(
                ProjectProductLink.project_id == source.project_id,
                Product.source_url == source.source_url
            ).order_by(Product.created_at.asc())
            result = await db.execute(stmt)
            products = result.scalars().all()
            if not products:
                mpns = source.source_metadata.get('mpns', [])
                if not mpns:
                    extracted_products = source.source_metadata.get(
                        'extracted_products', [])
                    mpn = source.source_metadata.get('mpn')
                    mpns = [p.get('mpn')
                            for p in extracted_products if p.get('mpn')]
                if mpns:
                    stmt = select(Product).join(ProjectProductLink).where(
                        ProjectProductLink.project_id == source.project_id,
                        Product.product_code.in_(mpns)
                    ).order_by(Product.created_at.asc())
                    result = await db.execute(stmt)
                    products = result.scalars().all()
            if not products:
                pdf_files = source.source_metadata.get('pdf_files', [])
                filenames = [f.get('filename')
                             for f in pdf_files if f.get('filename')]

                if filenames:
                    stmt = select(Product).join(ProjectProductLink).where(
                        ProjectProductLink.project_id == source.project_id,
                        Product.source_url.in_(filenames)
                    ).order_by(Product.created_at.asc())
                    result = await db.execute(stmt)
                    products = result.scalars().all()
            if not products:
                raise HTTPException(
                    status_code=404, detail="No enriched data found")

            project = await db.get(Project, source.project_id) if products else None
            project_name = project.name if project else None
            use_case_lower = (
                project.use_case or "").lower() if project else ""

            if source.source_url and source.source_url.endswith('.pdf'):
                output_filename = source.source_url.rsplit('.', 1)[0] + '.xlsx'
            elif source.source_type == "pdf_multi_pending":
                pdf_files = source.source_metadata.get('pdf_files', [])
                if pdf_files and len(pdf_files) > 0:
                    first_pdf = pdf_files[0].get('filename', 'extracted')
                    output_filename = first_pdf.rsplit('.', 1)[0] + '.xlsx'
                else:
                    output_filename = source.source_url.rsplit(
                        '.', 1)[0] + '.xlsx' if '.' in source.source_url else f"{source.source_url}.xlsx"
            elif source.source_type == "pdf_fresh_pending":
                output_filename = f"{project_name}_extracted.xlsx" if project_name else "fresh_extracted.xlsx"
            elif source.source_url and '.' in source.source_url:
                output_filename = source.source_url.rsplit('.', 1)[0] + '.xlsx'
            else:
                output_filename = f"{source.source_url}.xlsx" if source.source_url else "extracted.xlsx"
            if 'back filling' in use_case_lower or 'validation' in use_case_lower:
                MAX_ATTRIBUTES = 40
            else:
                MAX_ATTRIBUTES = 40
            logger.info(
                f"Using {MAX_ATTRIBUTES} attribute columns for use case: {project.use_case if project else 'Unknown'}")
            return await generate_products_excel(products, db, global_project_name=project_name, filename=output_filename)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Download Error: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Error generating download")


def clean_numeric_string(value):
    if value is None or value == '':
        return ''
    s = str(value).strip()
    if not s:
        return ''
    try:
        f = float(s)
        if f == int(f):
            return str(int(f))
    except (ValueError, OverflowError):
        pass
    return s


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
        logger.info(
            f"Project {projectId} operation_mode: {project.operation_mode}")
        if not project:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"Project {projectId} not found")
        if project.operation_mode == "enrichment":
            default_workflow_stage = "enrichment"
        elif project.operation_mode == "cleaning":
            default_workflow_stage = "cleaning"
        else:
            default_workflow_stage = "aggregation"
        logger.info(f"Setting workflow_stage to: {default_workflow_stage}")
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
        recent_cutoff = now_ist() - timedelta(hours=24)
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
        with_mpn_count = 0
        without_mpn_count = 0
        for r in rows:
            mpn = clean_numeric_string(r.get('mpn', ''))
            brand = str(r.get('brand', '')).strip()
            sku = str(r.get('sku', '')).strip()
            product_name = str(r.get('product_name', "")).strip()
            has_identifier = bool(mpn or sku or product_name)
            if brand and has_identifier:
                valid_rows.append(r)
                if mpn:
                    with_mpn_count += 1
                else:
                    without_mpn_count += 1
            else:
                rejected_count += 1
                logger.warning(
                    f"Rejected row: missing required fields Brand='{brand}', MPN='{mpn}', SKU='{sku}', Product_Name='{product_name}'")
        if rejected_count > 0:
            logger.info(
                f"Rejected {rejected_count} rows due to missing SKU, MPN, or Brand")
        if not valid_rows:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                detail="No valid rows found. Each row must contain Brand and at least one of MPN, SKU, or Product Name.")
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
        original_filename = file.filename or 'import'
        base_name = original_filename.rsplit(
            '.', 1)[0] if '.' in original_filename else original_filename
        source_url = re.sub(r'[^a-zA-Z0-9_-]', '_', base_name)
        new_source = Source(
            source_type="excel" if file_ext in ['.xlsx', '.xls'] else "csv",
            source_url=source_url,
            project_id=projectId,
            status="completed",
            uploaded_at=now_ist(),
            content_data=content,
            source_metadata={
                "file_hash": file_hash,
                "total": total_rows,
                "inferred_taxonomies": inferred_count,
                "processing_status": "pending",
                'with_mpn_count': with_mpn_count,
                'without_mpn_count': without_mpn_count,
                'rejected_count': rejected_count
            }
        )
        db.add(new_source)
        await db.commit()
        await db.refresh(new_source)
        created_count = 0
        updated_count = 0
        for idx, row in enumerate(rows):
            val_mpn = clean_numeric_string(row.get("mpn")) or None
            val_sku = clean_numeric_string(row.get("sku")) or None
            code = val_mpn or val_sku
            product = None
            is_reused = False

            if code:
                # Look for COMPLETED product globally
                stmt = select(Product).where(
                    Product.product_code == str(code),
                    Product.enrichment_status == "completed"
                ).order_by(Product.updated_at.desc())
                result = await db.execute(stmt)
                product = result.scalars().first()

                if product:
                    # Check if already linked to this project
                    link_stmt = select(ProjectProductLink).where(
                        ProjectProductLink.product_id == product.id,
                        ProjectProductLink.project_id == projectId
                    )
                    link_result = await db.execute(link_stmt)

                    if not link_result.scalars().first():
                        # Link existing product to this project
                        db.add(ProjectProductLink(
                            project_id=projectId,
                            product_id=product.id,
                            enrichment_status="pending"
                        ))
                        await db.flush()

                    is_reused = True
                    updated_count += 1

            if not is_reused:
                # ✅ Create product with ALL required fields in constructor
                product = Product(
                    product_code=str(code),
                    workflow_stage=default_workflow_stage,
                    created_at=now_ist(),
                    enrichment_status="pending",
                    # ✅ ADD ALL REQUIRED FIELDS HERE:
                    product_name=row.get("product_name") or row.get(
                        "name") or str(code) or "Unknown Product",
                    mpn=val_mpn,
                    sku=val_sku,
                    taxonomy=row.get("taxonomy"),
                    source_url=new_source.source_url,
                    category_1=row.get('category_1'),
                    category_2=row.get('category_2'),
                    category_3=row.get('category_3'),
                    category_4=row.get('category_4'),
                    category_5=row.get('category_5'),
                    category_6=row.get('category_6'),
                    category_7=row.get('category_7'),
                    category_8=row.get('category_8'),
                    gtin=row.get('gtin'),
                    ean=row.get('ean'),
                    upc=row.get('upc'),
                    unspc=row.get('unspc'),
                    product_type=row.get('product_type'),
                    parent_sku=row.get('parent_sku'),
                    lifecycle_stage=row.get('lifecycle_stage'),
                    launch_date=row.get('launch_date'),
                    discontinue_status=row.get('discontinue_status'),
                    weight_unit=row.get('weight_unit'),
                    dimension_unit=row.get('dimension_unit'),
                    currency=row.get("currency", "USD"),
                )

                # ✅ Handle numeric fields with error handling
                try:
                    if row.get('weight'):
                        product.weight = str(row['weight']).replace(',', '')
                    if row.get('length'):
                        product.length = str(row['length']).replace(',', '')
                    if row.get('width'):
                        product.width = str(row['width']).replace(',', '')
                    if row.get('height'):
                        product.height = str(row['height']).replace(',', '')
                    if row.get("base_price"):
                        product.base_price = float(str(row["base_price"]).replace(',', ''))
                except ValueError as e:
                    logger.warning(f"Error parsing numeric field for {code}: {e}")

                # ✅ Set brand/vendor/industry relationships BEFORE flush
                brand = await get_or_create_brand(db, row.get("brand"))
                if brand:
                    product.brand_id = brand.id
                    product.brand_name = brand.name
                    brand.product_count = (brand.product_count or 0) + 1
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

                # ✅ NOW add and flush to get product.id
                db.add(product)
                await db.flush()  # Product now has an ID

                # ✅ Create link AFTER product has an ID
                db.add(ProjectProductLink(
                    project_id=projectId,
                    product_id=product.id,
                    enrichment_status="pending"
                ))

                created_count += 1

            else:
                # Reused product - just increment counter
                updated_count += 1

            # ✅ Continue with attribute processing (this part is fine)
            dynamic_attrs = row.get('attributes', [])

            # Also try attribute_name1..40 columns (fallback)
            if not dynamic_attrs:
                for i in range(1, 41):
                    attr_name = row.get(f'attribute_name{i}')
                    if attr_name and str(attr_name).strip():
                        dynamic_attrs.append({
                            'name': str(attr_name).strip(),
                            'value': str(row.get(f'attribute_value{i}', '')).strip(),
                            'uom': str(row.get(f'attribute_uom{i}', '')).strip(),
                            'validation_value': str(row.get(f'validation_value{i}', '')).strip(),
                            'validation_uom': str(row.get(f'validation_uom{i}', '')).strip()
                        })

            # Ensure category exists and link product
            path_parts = []
            for i in range(1, 9):
                cat = getattr(product, f'category_{i}', None)
                if cat and str(cat).strip():
                    path_parts.append(str(cat).strip())

            category_id = None
            if path_parts:
                category_id = await ensure_category_from_path(db, path_parts)
                if category_id:
                    product.category_id = category_id
                    db.add(product)

            # Save attributes to normalized tables
            if dynamic_attrs:
                try:
                    await save_attributes_normalized(db, product, dynamic_attrs, category_id)
                except Exception as e:
                    logger.error(
                        f"Failed to normalize attributes for {product.product_code}: {e}")
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
                            # product_name=title or sku,
                            brand_name=brand,
                            mpn=sku,
                            # project_id=source.project_id,
                            source_url=source.source_url,
                            attributes=raw_attributes,
                            enrichment_status='pending',
                            completeness_score=10
                        )
                        db_session.add(product)
                        await db_session.flush()

                        # Link product to project
                        db_session.add(ProjectProductLink(
                            project_id=source.project_id,
                            product_id=product.id
                        ))
                        logger.info(f"Created product: {sku}")
                    else:
                        link_stmt = select(ProjectProductLink).where(
                            ProjectProductLink.product_id == product.id,
                            ProjectProductLink.project_id == source.project_id
                        )
                        link_result = await db_session.execute(link_stmt)
                        if not link_result.scalars().first():
                            db_session.add(ProjectProductLink(
                                project_id=source.project_id,
                                product_id=product.id
                            ))
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
                        extracted_at=now_ist()
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
async def trigger_aggregation(source_id: str, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_session), current_user: User = Depends(get_current_user)):
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
        background_tasks.add_task(run_aggregation_task, str(
            source.id), user_id=current_user.id)
        return {
            'status': 'accepted',
            'message': "Aggregation started in the background",
            'source_id': source.id,

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
            # stmt = select(Product).where(
            #     ProjectProductLink.project_id,  == source.project_id,
            #     Product.enrichment_status == 'pending'
            # )
            stmt = select(Product).join(ProjectProductLink).where(
                ProjectProductLink.project_id == source.project_id,
                Product.enrichment_status == 'pending'
            )
            result = await db_session.execute(stmt)
            products = result.scalars().all()
            successful = 0
            failed = 0
            total = len(products)
            logger.info(
                f"Starting aggregation task for source {source_id}, found {total} pending products.")
            for idx, product in enumerate(products):
                try:
                    display_id = product.product_code if (
                        product.mpn or product.sku) else product.product_name

                    logger.info(f"Aggregating {idx+1}/{total}: {display_id}")

                    # if product.dynamic_attributes:
                    #     primary_attr_names = [
                    #         attr['name'] for attr in product.dynamic_attributes
                    #         if isinstance(attr, dict) and attr.get('name')]
                    # if product.dynamic_attributes:
                    #     for attr in product.dynamic_attributes:
                    #         if isinstance(attr, dict) and attr.get('name'):
                    #             existing_data[attr['name']] = {
                    #                 'value': attr.get('value'),
                    #                 'uom': attr.get('uom') or attr.get('unit')
                    #             }
                    # logger.info(
                    #     f" EXISTING DATA BUILT for {product.product_code}:")
                    # logger.info(
                    #     f"   dynamic_attributes count: {len(product.dynamic_attributes) if product.dynamic_attributes else 0}")
                    # logger.info(
                    #     f"   existing_data keys: {list(existing_data.keys())}")
                    # logger.info(
                    #     f"   existing_data sample: {dict(list(existing_data.items())[:2])}")
                    # for k, v in existing_data.items():
                    #     logger.info(
                    #         f"  {k}: value='{v.get('value')}', uom='{v.get('uom')}'")
                    # if existing_data:
                    #     logger.info(
                    #         f" Excel attributes: {list(existing_data.keys())[:5]}")
                    # logger.info(
                    #     f"Primary attributes found in DB: {primary_attr_names}")
                    # primary_attr_names = []
                    # existing_data = {}
                    # try:
                    #     stmt = (
                    #         select(AttributeValue)
                    #         .join(ProductAttributeValueLinkModel,
                    #               ProductAttributeValueLinkModel.attribute_value_id == AttributeValue.id)
                    #         .where(ProductAttributeValueLinkModel.product_id == product.id)
                    #     )
                    #     result=await db_session.execute(stmt)
                    #     attr_values=result.scalars().all()
                    #     for av in attr_values:
                    #         attr_stmt=select(Attribute).where(Attribute.id==av.attribute_id)
                    #         attr_result=await db_session.execute(attr_stmt)
                    #         attribute = attr_result.scalars().first()
                    #         if attribute:
                    #             attr_name=attribute.attribute_name
                    #             primary_attr_names.append(attr_name)
                    #             existing_data[attr_name] = {
                    #                 'value': av.value,
                    #                 'uom': av.uom or ''
                    #             }
                    # except Exception as e:
                    #     logger.warning(f"Failed to read normalized attributes for {product.product_code}: {e}")
                    # Get category expected attributes
                    category_attrs = []
                    if product.category_id:
                        category_attrs = await get_category_expected_attributes(
                            db_session, product.category_id
                        )

                    # Get product's existing attributes
                    existing_data = {}
                    try:
                        stmt = (
                            select(AttributeValue)
                            .join(ProductAttributeValueLinkModel,
                                  ProductAttributeValueLinkModel.attribute_value_id == AttributeValue.id)
                            .where(ProductAttributeValueLinkModel.product_id == product.id)
                        )
                        result = await db_session.execute(stmt)
                        attr_values = result.scalars().all()
                        for av in attr_values:
                            attr_stmt = select(Attribute).where(
                                Attribute.id == av.attribute_id)
                            attr_result = await db_session.execute(attr_stmt)
                            attribute = attr_result.scalars().first()
                            if attribute:
                                attr_name = attribute.attribute_name
                                existing_data[attr_name] = {
                                    'value': av.value,
                                    'uom': av.uom or ''
                                }
                    except Exception as e:
                        logger.warning(
                            f"Failed to read existing attributes for {product.product_code}: {e}")

                    # Merge: category attrs + existing attrs (deduplicated, category first)
                    existing_names = set(existing_data.keys())
                    primary_attr_names = list(
                        category_attrs)  # Start with category
                    for name in existing_names:
                        if name not in primary_attr_names:
                            primary_attr_names.append(name)

                    if not primary_attr_names:
                        primary_attr_names = list(existing_names)

                    logger.info(f"  Category attrs: {category_attrs}")
                    logger.info(f"  Existing attrs: {list(existing_names)}")
                    logger.info(
                        f"EXISTING DATA BUILT for {product.product_code}:")
                    logger.info(
                        f"  primary_attr_names count: {len(primary_attr_names)}")
                    logger.info(
                        f"  existing_data keys: {list(existing_data.keys())}")
                    if existing_data:
                        logger.info(
                            f"  Excel attributes: {list(existing_data.keys())[:5]}")
                    logger.info(
                        f"Primary attributes found in DB: {primary_attr_names}")
                    if len(primary_attr_names) > 100:
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
                        if not product.sku:
                            product.sku = golden.get('sku')
                        if not product.product_code:
                            product.product_code = golden.get('mpn')
                        ai_attributes = golden.get('attributes', {})
                        product.enrichment_status = 'completed'
                        product.data_quality_score = 100.0

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
                            # if product.dynamic_attributes and "validation" in use_case:
                            #     for attr in product.dynamic_attributes:
                            #         if isinstance(attr, dict) and attr.get('name'):
                            #             attr_name = attr['name']
                            #             if attr_name in ai_data_for_merge:
                            #                 ai_val = ai_data_for_merge[attr_name]
                            #                 if isinstance(ai_val, dict):
                            #                     attr['validation_value'] = ai_val.get(
                            #                         'value', '')
                            #                     attr['validation_uom'] = ai_val.get(
                            #                         'unit', '') or ai_val.get('uom', '')
                            #                 else:
                            #                     attr['validation_value'] = str(
                            #                         ai_val) if ai_val else ''
                            if existing_data and "validation" in use_case:
                                for attr_name, attr_data in existing_data.items():
                                    if attr_name in ai_data_for_merge:
                                        ai_val = ai_data_for_merge[attr_name]
                                        if isinstance(ai_val, dict):
                                            validation_value = ai_val.get(
                                                'value', '')
                                            validation_uom = ai_val.get(
                                                'unit', '') or ai_val.get('uom', '')
                                        else:
                                            validation_value = str(
                                                ai_val) if ai_val else ''
                                            validation_uom = ''

                                        # Update validation values in attribute_value table
                                        try:
                                            val_stmt = select(AttributeValue).join(
                                                ProductAttributeValueLinkModel,
                                                ProductAttributeValueLinkModel.attribute_value_id == AttributeValue.id
                                            ).where(
                                                ProductAttributeValueLinkModel.product_id == product.id,
                                                AttributeValue.attribute_id == select(Attribute.id).where(
                                                    Attribute.attribute_name == attr_name
                                                ).scalar_subquery()
                                            )
                                            val_result = await db_session.execute(val_stmt)
                                            attr_val = val_result.scalars().first()

                                            if attr_val:
                                                attr_val.validation_value = validation_value
                                                attr_val.validation_uom = validation_uom
                                                db_session.add(attr_val)
                                        except Exception as e:
                                            logger.error(
                                                f"Failed to update validation for {attr_name}: {e}")
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
                            extracted_at=now_ist()
                        ))
                        flag_modified(product, "sources_consulted")
                        db_session.add(product)
                        await db_session.commit()
                        await db_session.get(Product, product.id)
                        try:
                            for attr_name, attr_value in ai_attributes.items():
                                value = attr_value.get('value') if isinstance(
                                    attr_value, dict) else str(attr_value)
                                uom = attr_value.get("unit", "") if isinstance(
                                    attr_value, dict) else ''
                                attr_stmt = select(Attribute).where(
                                    func.lower(
                                        Attribute.attribute_name) == attr_name.lower().strip()
                                )
                                attr_result = await db_session.execute(attr_stmt)
                                attribute = attr_result.scalars().first()
                                if not attribute:
                                    display_name = attr_name.strip()
                                    if display_name:
                                        display_name = display_name[0].upper(
                                        ) + display_name[1:] if len(display_name) > 1 else display_name.upper()
                                    attribute = Attribute(attribute_name=display_name, attribute_code=attr_name.lower(
                                    ).replace(" ", "_").replace('/', "_"), data_type="string")
                                    db_session.add(attribute)
                                    await db_session.flush()
                                link_stmt = select(ProductAttributeLinkModel).where(
                                    ProductAttributeLinkModel.product_id == product.id, ProductAttributeLinkModel.attribute_id == attribute.id,)
                                if not (await db_session.execute(link_stmt)).scalars().first():
                                    db_session.add(ProductAttributeLinkModel(
                                        product_id=product.id, attribute_id=attribute.id))
                                val_stmt = select(AttributeValue).where(
                                    AttributeValue.attribute_id == attribute.id, AttributeValue.value == str(value))
                                val_result = await db_session.execute(val_stmt)
                                attr_value_obj = val_result.scalars().first()
                                if not attr_value_obj:
                                    attr_value_obj = AttributeValue(
                                        attribute_id=attribute.id,
                                        value=str(value),
                                        uom=str(uom) if uom else None,
                                    )
                                    db_session.add(attr_value_obj)
                                    await db_session.flush()

                                # Link Product → AttributeValue (always run)
                                pv_stmt = select(ProductAttributeValueLinkModel).where(
                                    ProductAttributeValueLinkModel.product_id == product.id,
                                    ProductAttributeValueLinkModel.attribute_value_id == attr_value_obj.id,
                                )
                                if not (await db_session.execute(pv_stmt)).scalars().first():
                                    db_session.add(ProductAttributeValueLinkModel(
                                        product_id=product.id,
                                        attribute_value_id=attr_value_obj.id
                                    ))

                                # Link new attribute to the product's category (always run)
                                if product.category_id:
                                    from app.models.attribute import CategoryAttribute
                                    ca_stmt = select(CategoryAttribute).where(
                                        CategoryAttribute.category_id == product.category_id,
                                        CategoryAttribute.attribute_id == attribute.id,
                                    )
                                    if not (await db_session.execute(ca_stmt)).scalars().first():
                                        db_session.add(CategoryAttribute(
                                            category_id=product.category_id,
                                            attribute_id=attribute.id,
                                        ))

                        except Exception as e:
                            logger.error(
                                f"Failed to sync AI attributes to normalized tables: {e}")

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
                "last_aggregation_time": now_ist().isoformat()
            })
            source.source_metadata = meta
            db_session.add(source)
            await db_session.commit()
            logger.info(
                f"Aggregation complete: {successful}/{total} successful, {failed} failed")
        except Exception as e:
            await db_session.rollback()
            logger.error(f"Aggregation task failed for {source_id}: {str(e)}")
