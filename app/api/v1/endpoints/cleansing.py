from typing import Dict, List
from datetime import datetime
from sqlalchemy import or_
from app.models.cleaning import CleaningTask
from app.models.product import Product
from app.models.project import Project
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, func, and_,case
from app.core.database import get_session
from app.models.pipeline import CleansingIssue, Source
import logging
from app.core.database import async_session_factory
from sqlalchemy.orm.attributes import flag_modified
from app.aggregation.services.cleaning_service import AttributeInput, LLMCleaningResponse, LLMCleaningService, ProductContext
import uuid
from fastapi.responses import StreamingResponse
import pandas as pd
import io
from app.schemas.aggregation import AggregateLLMRequest, UpdateAttributesRequest
from app.schemas.enrichment import AggregatedAttribute
from app.schemas.cleaning import BulkUpdateAttributesRequest, ExportSelectedCleaningRequest, RunCleaningRequest
from app.utils.aggregate_download import generate_products_excel
from app.utils.cleaning_helper import append_cleaning_task_log, create_cleaning_task, get_cleaning_task_or_404, update_cleaning_task_status
logger = logging.getLogger("cleansing_router")
router = APIRouter()
@router.get("/issues")
async def get_all_issues(db: AsyncSession = Depends(get_session)):
    try:
        statement = select(CleansingIssue).order_by(
            CleansingIssue.detected_at.desc())
        result = await db.execute(statement)
        return result.scalars().all()
    except Exception as e:
        logger.error(f"Failed to fetch cleansing issues: {e}")
        return []
@router.post("/resolve/{issue_id}")
async def resolve_issue(issue_id: str, db: AsyncSession = Depends(get_session)):
    try:
        issue = await db.get(CleansingIssue, issue_id)
        if not issue:
            raise HTTPException(status_code=404, detail="Issue not found")
        issue.resolved = True
        db.add(issue)
        await db.commit()
        return {"status": "success"}
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    
# async def run_cleaning_task(
#     project_id: str,
#     product_ids: List[str],
#     task_id: str,
#     llm_provider: str,
# ):
#     async with async_session_factory() as db:
#         try:
#             await update_cleaning_task_status(db, task_id, "running")
#             await append_cleaning_task_log(db, task_id, f"Starting cleaning for project {project_id}")
#             service = LLMCleaningService(
#                 llm_provider=llm_provider,
#                 db=db,
#                 project_id=project_id,
#                 concurrency_limit=10
#             )
#             if product_ids:
#                 stmt = select(Product).where(Product.id.in_(product_ids))
#                 await append_cleaning_task_log(
#                     db, task_id, f"Cleaning {len(product_ids)} selected product(s)"
#                 )
#             else:
#                 stmt = select(Product).where(Product.project_id == project_id)
#                 await append_cleaning_task_log(
#                     db, task_id, "Cleaning all products in project"
#                 )
#             result = await db.execute(stmt)
#             products = result.scalars().all()
#             if not products:
#                 await append_cleaning_task_log(db, task_id, "No products found")
#                 await update_cleaning_task_status(db, task_id, "failed", "No products found")
#                 return
#             total = len(products)
#             updated_count = 0
#             failed_count = 0
#             for product in products:
#                 try:
#                     product.enrichment_status = "processing"
#                     product.updated_at = datetime.utcnow()
#                     db.add(product)
#                     await db.commit()
#                     await db.refresh(product)
#                     if not product.dynamic_attributes:
#                         product.enrichment_status = "completed"
#                         product.updated_at = datetime.utcnow()
#                         db.add(product)
#                         await db.commit()
#                         continue
#                     attributes = []
#                     for attr_idx, attr in enumerate(product.dynamic_attributes):
#                         if attr.get("value"):
#                             attributes.append(
#                                 AttributeInput(
#                                     id=str(attr_idx),
#                                     name=attr.get("name", ""),
#                                     value=attr.get("value", ""),
#                                     unit=attr.get("unit") or attr.get("uom"),
#                                     source="existing_data",
#                                 )
#                             )
#                     if not attributes:
#                         product.enrichment_status = "completed"
#                         product.updated_at = datetime.utcnow()
#                         db.add(product)
#                         await db.commit()
#                         continue
#                     context = ProductContext(
#                         mpn=product.product_code,
#                         brand=product.brand_name,
#                         product_name=product.product_name,
#                         taxonomy=product.taxonomy,
#                     )
#                     await append_cleaning_task_log(
#                         db,
#                         task_id,
#                         f"Cleaning product {product.product_code or product.id} with {len(attributes)} attribute(s)",
#                     )
#                     cleaning_result = await service.clean_attributes(attributes, context)
#                     updated = await save_cleaned_attributes(
#                         db, product.id, cleaning_result, product.dynamic_attributes
#                     )
#                     product.enrichment_status = "completed"
#                     product.updated_at = datetime.utcnow()
#                     db.add(product)
#                     await db.commit()
#                     if updated:
#                         updated_count += 1
#                 except Exception as product_error:
#                     failed_count += 1
#                     logger.error(
#                         f"Failed cleaning product {product.id}: {product_error}",
#                         exc_info=True,
#                     )
#                     try:
#                         product.enrichment_status = "failed"
#                         product.updated_at = datetime.utcnow()
#                         db.add(product)
#                         await db.commit()
#                     except Exception as commit_error:
#                         await db.rollback()
#                         logger.error(
#                             f"Failed updating failed status for product {product.id}: {commit_error}",
#                             exc_info=True,
#                         )
#                     try:
#                         await append_cleaning_task_log(
#                             db,
#                             task_id,
#                             f"Failed cleaning product {product.product_code or product.id}: {str(product_error)}",
#                         )
#                     except Exception as log_error:
#                         logger.error(
#                             f"Failed writing product error log for task {task_id}: {log_error}",
#                             exc_info=True,
#                         )
#             await append_cleaning_task_log(
#                 db,
#                 task_id,
#                 f"Cleaning completed. Updated {updated_count}/{total}, failed {failed_count}",
#             )
#             try:
#                 stmt = select(Source).where(Source.project_id == project_id)
#                 result = await db.execute(stmt)
#                 sources = result.scalars().all()
#                 for source in sources:
#                     metadata = dict(source.source_metadata or {})
#                     metadata["processing_status"] = "completed"
#                     source.source_metadata = metadata
#                     db.add(source)
#                 project = await db.get(Project, project_id)
#                 if project:
#                     status_stmt = select(
#                         func.count(Product.id),
#                         func.sum(case((Product.enrichment_status == "completed", 1), else_=0)),
#                         func.sum(case((Product.enrichment_status == "failed", 1), else_=0)),
#                         func.sum(case((Product.enrichment_status == "processing", 1), else_=0)),
#                         func.sum(case((Product.enrichment_status == "pending", 1), else_=0)),
#                     ).where(Product.project_id == project_id)
#                     status_result = await db.execute(status_stmt)
#                     row = status_result.one()
#                     total = row[0] or 0
#                     completed = row[1] or 0
#                     failed = row[2] or 0
#                     processing = row[3] or 0
#                     pending = row[4] or 0
#                     if total == 0:
#                         project.status = "draft"
#                     elif processing > 0:
#                         project.status = "processing"
#                     elif completed == total:
#                         project.status = "completed"
#                     elif failed == total:
#                         project.status = "failed"
#                     elif completed > 0:
#                         project.status = "partially_completed"
#                     else:
#                         project.status = "draft"
#                     db.add(project)
#                 await db.commit()
#             except Exception as status_error:
#                 await db.rollback()
#                 logger.error(
#                     f"Failed to update source/project failure status for project {project_id}: {status_error}",
#                     exc_info=True,
#                 )
#             except Exception as e:
#                 raise e
#             await update_cleaning_task_status(db, task_id, "completed")
#         except Exception as e:
#             logger.error(f"Cleaning task {task_id} failed: {e}", exc_info=True)
#             try:
#                 await db.rollback()
#             except Exception:
#                 logger.exception(
#                     "Rollback failed during cleaning task failure")
#             try:
#                 await append_cleaning_task_log(
#                     db,
#                     task_id,
#                     f"Task failed: {str(e)}",
#                 )
#             except Exception:
#                 logger.exception("Failed to append failure log")
#             try:
#                 await update_cleaning_task_status(db, task_id, "failed", str(e))
#             except Exception:
#                 logger.exception("Failed to mark task as failed")
    
async def run_cleaning_task(
    project_id: str,
    product_ids: List[str],
    task_id: str,
    llm_provider: str,
):
    async with async_session_factory() as db:
        try:
            await update_cleaning_task_status(db, task_id, "running")
            await append_cleaning_task_log(db, task_id, f"Starting cleaning for project {project_id}")
            service = LLMCleaningService(
                llm_provider=llm_provider,
                db=db,
                project_id=project_id,
                concurrency_limit=10
            )
            if product_ids:
                stmt = select(Product).where(Product.id.in_(product_ids))
                await append_cleaning_task_log(
                    db, task_id, f"Cleaning {len(product_ids)} selected product(s)"
                )
            else:
                stmt = select(Product).where(Product.project_id == project_id)
                await append_cleaning_task_log(
                    db, task_id, "Cleaning all products in project"
                )
            result = await db.execute(stmt)
            products = result.scalars().all()
            if not products:
                await append_cleaning_task_log(db, task_id, "No products found")
                await update_cleaning_task_status(db, task_id, "failed", "No products found")
                return
            total = len(products)
            await append_cleaning_task_log(db, task_id, "Pass 1: Collecting all attribute names...")
            
            all_attribute_names = set()
            for product in products:
                if product.dynamic_attributes:
                    for attr in product.dynamic_attributes:
                        name = attr.get("name", "").strip()
                        if name:
                            all_attribute_names.add(name)
            
            await append_cleaning_task_log(db, task_id, f"Found {len(all_attribute_names)} unique attribute names")
            
            # ========== PASS 2: Get global name mapping from LLM ==========
            await append_cleaning_task_log(db, task_id, "Pass 2: Creating global attribute name mapping...")
            
            global_mapping = await service.get_global_name_mapping(
                list(all_attribute_names),
                project_id
            )
            
            if global_mapping:
                await append_cleaning_task_log(db, task_id, f"Global mapping created with {len(global_mapping)} entries")
                
                # Apply mapping to all products
                for product in products:
                    if product.dynamic_attributes:
                        updated = False
                        new_attrs = []
                        for attr in product.dynamic_attributes:
                            attr_copy = dict(attr)
                            old_name = attr_copy.get("name", "")
                            if old_name in global_mapping:
                                new_name = global_mapping[old_name]
                                if new_name != old_name:
                                    attr_copy["name"] = new_name
                                    updated = True
                            new_attrs.append(attr_copy)
                        
                        if updated:
                            product.dynamic_attributes = new_attrs
                            flag_modified(product, "dynamic_attributes")
                            db.add(product)
                
                await db.commit()
                await append_cleaning_task_log(db, task_id, "Global name mapping applied to all products")
            else:
                await append_cleaning_task_log(db, task_id, "No global mapping created, proceeding with individual cleaning")
            
            # ========== PASS 3: Clean individual attribute values ==========
            await append_cleaning_task_log(db, task_id, "Pass 3: Cleaning individual attribute values...")
            updated_count = 0
            failed_count = 0
            for product in products:
                try:
                    product.enrichment_status = "processing"
                    product.updated_at = datetime.utcnow()
                    db.add(product)
                    await db.commit()
                    await db.refresh(product)
                    if not product.dynamic_attributes:
                        product.enrichment_status = "completed"
                        product.updated_at = datetime.utcnow()
                        db.add(product)
                        await db.commit()
                        continue
                    attributes = []
                    for attr_idx, attr in enumerate(product.dynamic_attributes):
                        if attr.get("value"):
                            attributes.append(
                                AttributeInput(
                                    id=str(attr_idx),
                                    name=attr.get("name", ""),
                                    value=attr.get("value", ""),
                                    unit=attr.get("unit") or attr.get("uom"),
                                    source="existing_data",
                                )
                            )
                    if not attributes:
                        product.enrichment_status = "completed"
                        product.updated_at = datetime.utcnow()
                        db.add(product)
                        await db.commit()
                        continue
                    context = ProductContext(
                        mpn=product.product_code,
                        brand=product.brand_name,
                        product_name=product.product_name,
                        taxonomy=product.taxonomy,
                    )
                    await append_cleaning_task_log(
                        db,
                        task_id,
                        f"Cleaning product {product.product_code or product.id} with {len(attributes)} attribute(s)",
                    )
                    cleaning_result = await service.clean_attributes(attributes, context)
                    updated = await save_cleaned_attributes(
                        db, product.id, cleaning_result, product.dynamic_attributes
                    )
                    product.enrichment_status = "completed"
                    product.updated_at = datetime.utcnow()
                    db.add(product)
                    await db.commit()
                    if updated:
                        updated_count += 1
                except Exception as product_error:
                    failed_count += 1
                    logger.error(
                        f"Failed cleaning product {product.id}: {product_error}",
                        exc_info=True,
                    )
                    try:
                        product.enrichment_status = "failed"
                        product.updated_at = datetime.utcnow()
                        db.add(product)
                        await db.commit()
                    except Exception as commit_error:
                        await db.rollback()
                        logger.error(
                            f"Failed updating failed status for product {product.id}: {commit_error}",
                            exc_info=True,
                        )
                    try:
                        await append_cleaning_task_log(
                            db,
                            task_id,
                            f"Failed cleaning product {product.product_code or product.id}: {str(product_error)}",
                        )
                    except Exception as log_error:
                        logger.error(
                            f"Failed writing product error log for task {task_id}: {log_error}",
                            exc_info=True,
                        )
            await append_cleaning_task_log(
                db,
                task_id,
                f"Cleaning completed. Updated {updated_count}/{total}, failed {failed_count}",
            )
            try:
                stmt = select(Source).where(Source.project_id == project_id)
                result = await db.execute(stmt)
                sources = result.scalars().all()
                for source in sources:
                    metadata = dict(source.source_metadata or {})
                    metadata["processing_status"] = "completed"
                    source.source_metadata = metadata
                    db.add(source)
                project = await db.get(Project, project_id)
                if project:
                    status_stmt = select(
                        func.count(Product.id),
                        func.sum(case((Product.enrichment_status == "completed", 1), else_=0)),
                        func.sum(case((Product.enrichment_status == "failed", 1), else_=0)),
                        func.sum(case((Product.enrichment_status == "processing", 1), else_=0)),
                        func.sum(case((Product.enrichment_status == "pending", 1), else_=0)),
                    ).where(Product.project_id == project_id)
                    status_result = await db.execute(status_stmt)
                    row = status_result.one()
                    total = row[0] or 0
                    completed = row[1] or 0
                    failed = row[2] or 0
                    processing = row[3] or 0
                    pending = row[4] or 0
                    if total == 0:
                        project.status = "draft"
                    elif processing > 0:
                        project.status = "processing"
                    elif completed == total:
                        project.status = "completed"
                    elif failed == total:
                        project.status = "failed"
                    elif completed > 0:
                        project.status = "partially_completed"
                    else:
                        project.status = "draft"
                    db.add(project)
                await db.commit()
            except Exception as status_error:
                await db.rollback()
                logger.error(
                    f"Failed to update source/project failure status for project {project_id}: {status_error}",
                    exc_info=True,
                )
            except Exception as e:
                raise e
            await update_cleaning_task_status(db, task_id, "completed")
        except Exception as e:
            logger.error(f"Cleaning task {task_id} failed: {e}", exc_info=True)
            try:
                await db.rollback()
            except Exception:
                logger.exception(
                    "Rollback failed during cleaning task failure")
            try:
                await append_cleaning_task_log(
                    db,
                    task_id,
                    f"Task failed: {str(e)}",
                )
            except Exception:
                logger.exception("Failed to append failure log")
            try:
                await update_cleaning_task_status(db, task_id, "failed", str(e))
            except Exception:
                logger.exception("Failed to mark task as failed")
                
async def add_log(db: AsyncSession, task_id: str, message: str):
    task = await db.get(CleaningTask, task_id)
    if not task:
        return
    timestamp = datetime.utcnow().isoformat()
    logs = task.logs or []
    logs.append(f"{timestamp} - {message}")
    task.logs = logs
    task.updated_at = datetime.utcnow()
    db.add(task)
    await db.commit()
    
@router.post("/run")
async def run_cleaning(
    request: RunCleaningRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session)
):
    try:
        project = await db.get(Project, request.project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        task_id = str(uuid.uuid4())
        await create_cleaning_task(db, task_id)
        background_tasks.add_task(
            run_cleaning_task,
            request.project_id,
            request.product_ids or [],
            task_id,
            request.llm_provider,
        )
        return {"status": "accepted", "task_id": task_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to start cleaning task: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Failed to start cleaning task")
        
@router.get("/tasks/{task_id}")
async def get_task_status(task_id: str, db: AsyncSession = Depends(get_session)):
    try:
        task = await get_cleaning_task_or_404(db, task_id)
        return {
            "status": task.status,
            "logs": task.logs or [],
            "error": task.error,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error getting task status for {task_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Failed to fetch task status")
        
async def save_cleaned_attributes(
    db_session: AsyncSession,
    product_id: str,
    cleaning_response: LLMCleaningResponse,
    original_attributes: List[Dict]
) -> bool:
    try:
        product = await db_session.get(Product, product_id)
        if not product or not product.dynamic_attributes:
            return False
        cleaned_by_id = {
            str(ca.id): ca for ca in cleaning_response.cleaned_attributes
        }
        updated = False
        new_attrs = [dict(a) for a in product.dynamic_attributes]
        for idx, attr in enumerate(new_attrs):
            attr_id = str(idx)
            cleaned = cleaned_by_id.get(attr_id)
            if not cleaned:
                continue
            name_changed = cleaned.name != attr.get("name")
            final_val = str(cleaned.cleaned_value)
            final_unit = cleaned.unit or ""
            if final_unit and final_val.endswith(f" {final_unit}"):
                final_val = final_val.replace(f" {final_unit}", "").strip()
            val_changed = final_val != str(attr.get("value"))
            unit_changed = final_unit != (attr.get("unit") or attr.get("uom"))
            if name_changed or val_changed or unit_changed:
                logger.info(
                    f"Updating {attr.get('name')}: {attr.get('value')} -> {cleaned.cleaned_value}"
                )
                attr["name"] = cleaned.name
                attr["value"] = final_val
                attr["unit"] = final_unit
                updated = True
        if updated:
            product.dynamic_attributes = new_attrs
            flag_modified(product, "dynamic_attributes")
            product.updated_at = datetime.utcnow()
            db_session.add(product)
        return updated
    except Exception as e:
        logger.error(
            f"Failed saving cleaned attributes for product {product_id}: {e}", exc_info=True)
        raise
    
@router.get("/tasks/{task_id}/logs")
async def get_task_logs(task_id: str, db: AsyncSession = Depends(get_session)):
    try:
        task = await get_cleaning_task_or_404(db, task_id)
        return {"logs": task.logs or []}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error getting task logs for {task_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Failed to fetch task logs")
        
@router.get("/projects/{project_id}/download")
async def download_cleaned_project(
    project_id: str,
    db: AsyncSession = Depends(get_session)
):
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    stmt = select(Product).where(Product.project_id ==
                                 project_id).order_by(Product.created_at.asc())
    result = await db.execute(stmt)
    products = result.scalars().all()
    if not products:
        raise HTTPException(status_code=404, detail="No products found")
    MAX_ATTRIBUTES = 40
    base_headers = [
        "Prod ID", "SKU", "Product_Type", "Parent_SKU", "Product_Name",
        "Brand", "GTIN", "ean", "upc", "unspc", "MPN", "Status",
        "Lifecycle_Stage", "Launch_Date", "Discontinue_Status",
        "industry_name", "category 1", "category 2", "category 3", "category 4",
        "category 5", "category 6", "category 7", "category 8", "Taxonomy",
        "Country_of_Origin", "Warranty", "Weight", "Weight_Unit",
        "Length", "Width", "Height", "Dimension_Unit", "Variant_Status",
        "Currency", "Base Price", "Sale Price", "Selling_Price",
        "Special_Price", "Stock_Qty", "Stock_Status", "Vendor_Name", "Vendor_SKU"
    ]
    media_headers = []
    for i in range(1, 9):
        media_headers.append(f"image_name_{i}")
        media_headers.append(f"image_url_{i}")
    for i in range(1, 4):
        media_headers.append(f"video_name_{i}")
        media_headers.append(f"video_url_{i}")
    for i in range(1, 6):
        media_headers.append(f"document_name_{i}")
        media_headers.append(f"document_url_{i}")
    content_headers = [
        "3D_Model_URL", "Short_Description", "Long_Description",
        "features_1", "features_2", "features_3", "features_4", "features_5",
        "features_6", "features_7", "features_8", "features_9", "features_10",
        "Meta_Title", "Meta_Description", "Search_Keywords",
        "Certification", "Safety_Standard", "Hazardous_Material", "Prop65_Warning"
    ]
    attr_headers = []
    for i in range(1, MAX_ATTRIBUTES + 1):
        attr_headers.append(f"attribute_name{i}")
        attr_headers.append(f"attribute_value{i}")
        attr_headers.append(f"attribute_uom{i}")
    source_url_headers = [f"source_url_{i}" for i in range(1, 6)]
    all_headers = base_headers + media_headers + \
        content_headers + attr_headers + source_url_headers
    rows = []
    for product in products:
        row = {h: "" for h in all_headers}
        row["Prod ID"] = str(product.id) if product.id else ""
        row["SKU"] = product.sku or ""
        row["Product_Name"] = product.product_name or ""
        row["Brand"] = product.brand_name or ""
        row["MPN"] = product.product_code or ""
        row["Taxonomy"] = product.taxonomy or ""
        row["industry_name"] = product.industry_name or ""
        row["category 1"] = product.category_1 or ""
        row["category 2"] = product.category_2 or ""
        row["category 3"] = product.category_3 or ""
        row["category 4"] = product.category_4 or ""
        row["category 5"] = product.category_5 or ""
        row["category 6"] = product.category_6 or ""
        row["category 7"] = product.category_7 or ""
        row["category 8"] = product.category_8 or ""
        row["Weight"] = str(product.weight) if product.weight else ""
        row["Weight_Unit"] = product.weight_unit or ""
        row["Length"] = str(product.length) if product.length else ""
        row["Width"] = str(product.width) if product.width else ""
        row["Height"] = str(product.height) if product.height else ""
        row["Dimension_Unit"] = product.dimension_unit or ""
        row["Currency"] = product.currency or ""
        row["Base Price"] = str(
            product.base_price) if product.base_price else ""
        row["Vendor_Name"] = product.vendor_name or ""
        row["Short_Description"] = product.short_description or ""
        row["Long_Description"] = product.long_description or ""
        if product.features and isinstance(product.features, list):
            for i, feat in enumerate(product.features[:10], 1):
                row[f"features_{i}"] = feat
        for i in range(1, 9):
            url = getattr(product, f"image_url_{i}", None)
            if url:
                row[f"image_url_{i}"] = url
        if product.sources_consulted and isinstance(product.sources_consulted, list):
            for i, url in enumerate(product.sources_consulted[:5], 1):
                row[f"source_url_{i}"] = url
        if product.dynamic_attributes:
            for idx, attr in enumerate(product.dynamic_attributes[:MAX_ATTRIBUTES]):
                i = idx + 1
                row[f"attribute_name{i}"] = attr.get("name", "")
                row[f"attribute_value{i}"] = attr.get("value", "")
                row[f"attribute_uom{i}"] = attr.get(
                    "unit") or attr.get("uom") or ""
        rows.append(row)
    df = pd.DataFrame(rows, columns=all_headers)
    excel_buffer = io.BytesIO()
    with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Cleaned Data')
    excel_buffer.seek(0)
    filename = f"Cleaned_Project_{project.name}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return StreamingResponse(
        excel_buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )
@router.put("/products/{product_id}/attributes")
async def update_product_attributes(
    product_id: str,
    request: UpdateAttributesRequest,
    db: AsyncSession = Depends(get_session)
):
    try:
        product = await db.get(Product, product_id)
        if not product:
            raise HTTPException(status_code=404, detail="Product not found")
        project_id = product.project_id
        cleaning_service = LLMCleaningService(
            llm_provider="openai",
            db=db,
            project_id=project_id
        )
        if product.dynamic_attributes:
            for attr in product.dynamic_attributes:
                attr_name = attr.get('name')
                if attr_name in request.attributes:
                    incoming = request.attributes[attr_name]
                    attribute_input = AttributeInput(
                        id="0",
                        name=attr_name,
                        value=incoming.value,
                        unit=incoming.uom or attr.get("unit") or attr.get("uom"),
                        source="manual_update"
                    )
                    context = ProductContext(
                        mpn=product.product_code,
                        brand=product.brand_name,
                        product_name=product.product_name,
                        taxonomy=product.taxonomy
                    )
                    try:
                        cleaning_result = await cleaning_service.clean_attributes(
                            [attribute_input], 
                            context
                        )
                        if cleaning_result.cleaned_attributes:
                            cleaned = cleaning_result.cleaned_attributes[0]
                            attr["value"] = cleaned.cleaned_value
                            attr["unit"] = cleaned.unit or incoming.uom or ""
                            attr["uom"] = cleaned.unit or incoming.uom or ""
                        else:
                            attr["value"] = incoming.value
                            attr["unit"] = incoming.uom or ""
                            attr["uom"] = incoming.uom or ""
                    except Exception as e:
                        logger.error(f"Cleaning failed for attribute {attr_name}: {e}")
                        attr["value"] = incoming.value
                        attr["unit"] = incoming.uom or ""
                        attr["uom"] = incoming.uom or ""
                    if (
                        product.validation_conflicts
                        and attr_name in product.validation_conflicts
                    ):
                        del product.validation_conflicts[attr_name]
        flag_modified(product, "dynamic_attributes")
        if product.validation_conflicts:
            flag_modified(product, "validation_conflicts")
        product.updated_at = datetime.utcnow()
        db.add(product)
        await db.commit()
        return {"status": "success", "message": "Attributes updated"}
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to update attributes: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
@router.put("/products/bulk-attributes")
async def bulk_update_product_attributes(
    request: BulkUpdateAttributesRequest,
    db: AsyncSession = Depends(get_session)
):
    try:
        if not request.product_ids:
            raise HTTPException(status_code=400, detail="No product IDs provided")
        if not request.attributes:
            raise HTTPException(status_code=400, detail="No attributes provided")
        stmt = select(Product).where(Product.id.in_(request.product_ids))
        result = await db.execute(stmt)
        products = result.scalars().all()
        if not products:
            raise HTTPException(status_code=404, detail="No products found")
        project_id = products[0].project_id if products else None
        cleaning_service = LLMCleaningService(
            llm_provider="openai",
            db=db,
            project_id=project_id
        )
        updated_count = 0
        for product in products:
            if not product.dynamic_attributes:
                continue
            updated = False
            new_attrs = [dict(attr) for attr in product.dynamic_attributes]
            context = ProductContext(
                mpn=product.product_code,
                brand=product.brand_name,
                product_name=product.product_name,
                taxonomy=product.taxonomy
            )
            for idx, attr in enumerate(new_attrs):
                attr_name = attr.get("name")
                if attr_name not in request.attributes:
                    continue
                raw_value = request.attributes[attr_name]
                attribute_input = AttributeInput(
                    id=str(idx),
                    name=attr_name,
                    value=raw_value,
                    unit=attr.get("unit") or attr.get("uom"),
                    source="bulk_update"
                )
                try:
                    cleaning_result = await cleaning_service.clean_attributes(
                        [attribute_input],
                        context
                    )
                    if cleaning_result.cleaned_attributes:
                        cleaned = cleaning_result.cleaned_attributes[0]
                        attr["value"] = cleaned.cleaned_value
                        attr["unit"] = cleaned.unit or ""
                        attr["uom"] = cleaned.unit or ""
                    else:
                        attr["value"] = raw_value
                except Exception as e:
                    logger.error(
                        f"Bulk cleaning failed for product {product.id}, attribute {attr_name}: {e}",
                        exc_info=True,
                    )
                    attr["value"] = raw_value
                updated = True
            if updated:
                product.dynamic_attributes = new_attrs
                flag_modified(product, "dynamic_attributes")
                if product.validation_conflicts:
                    for attr_name in request.attributes.keys():
                        if attr_name in product.validation_conflicts:
                            del product.validation_conflicts[attr_name]
                    flag_modified(product, "validation_conflicts")
                product.updated_at = datetime.utcnow()
                db.add(product)
                updated_count += 1
        await db.commit()
        return {
            "status": "success",
            "message": f"Updated {updated_count} product(s)",
            "updated_count": updated_count
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Bulk update attributes failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to bulk update attributes"
        )
        
@router.post("/download-selected")
async def download_selected_cleaned_products(
    request: ExportSelectedCleaningRequest,
    db: AsyncSession = Depends(get_session)
):
    try:
        if not request.product_ids and not request.project_ids:
            raise HTTPException(status_code=400, detail="No products or projects selected")
        stmt = select(Product)
        filters = []
        if request.product_ids:
            filters.append(Product.id.in_(request.product_ids))
        if request.project_ids:
            filters.append(Product.project_id.in_(request.project_ids))
        stmt = stmt.where(or_(*filters)).order_by(Product.created_at.asc())
        result = await db.execute(stmt)
        products = result.scalars().all()
        if not products:
            raise HTTPException(status_code=404, detail="No products found")
        return await generate_products_excel(products, db)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to download selected cleaned products: {e}",
            exc_info=True
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to download cleaned products"
        )