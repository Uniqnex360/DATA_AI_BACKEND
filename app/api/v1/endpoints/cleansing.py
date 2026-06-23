from typing import Dict, List
from datetime import datetime, timedelta, timezone
from openai import project
from sqlalchemy import or_
from app.models.attribute import Attribute, AttributeValue
from app.models.attribute_edit_log import AttributeEditLog
from app.models.cleaning import CleaningTask
from app.models.product import Product
from app.models.product_attribute_link import ProductAttributeLinkModel, ProductAttributeValueLinkModel
from app.models.project import Project
from app.api.v1.endpoints.aggregation import update_project_status
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, func, and_, case
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
from app.models.project_product_link import ProjectProductLink
from app.schemas.aggregation import AggregateLLMRequest, UpdateAttributesRequest
from app.schemas.enrichment import AggregatedAttribute
from app.schemas.cleaning import BulkUpdateAttributesRequest, ExportSelectedCleaningRequest, RunCleaningRequest
from app.utils.aggregate_download import generate_products_excel
from app.utils.cleaning_helper import append_cleaning_task_log, create_cleaning_task, get_cleaning_task_or_404, update_cleaning_task_status
from app.utils.timezone import now_ist
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
            await update_project_status(db, project_id)
            await append_cleaning_task_log(db, task_id, "Project status updated to processing")
            await db.commit()
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
                stmt = select(Product).join(
                    ProjectProductLink, Product.id == ProjectProductLink.product_id
                ).where(
                    ProjectProductLink.project_id == project_id
                )
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
                try:
                    attr_stmt = (
                        select(Attribute.attribute_name)
                        .join(ProductAttributeLinkModel, ProductAttributeLinkModel.attribute_id == Attribute.id)
                        .where(ProductAttributeLinkModel.product_id == product.id)
                    )
                    attr_result = await db.execute(attr_stmt)
                    for row in attr_result.all():
                        all_attribute_names.add(row[0])
                except Exception as e:
                    logger.warning(
                        f"Failed to read attributes for {product.id}: {e}")
            await append_cleaning_task_log(db, task_id, f"Found {len(all_attribute_names)} unique attribute names")
            await append_cleaning_task_log(db, task_id, "Pass 2: Creating global attribute name mapping...")
            global_mapping = await service.get_global_name_mapping(
                list(all_attribute_names),
                project_id
            )
            if global_mapping:
                await append_cleaning_task_log(db, task_id, f"Global mapping created with {len(global_mapping)} entries")
                for product in products:
                    try:
                        for old_name, new_name in global_mapping.items():
                            if old_name == new_name:
                                continue
                            attr_stmt = select(Attribute).where(
                                Attribute.attribute_name == old_name)
                            attr_result = await db.execute(attr_stmt)
                            attribute = attr_result.scalars().first()
                            if attribute:
                                attribute.attribute_name = new_name
                                db.add(attribute)
                    except Exception as e:
                        logger.warning(
                            f"Failed to apply global mapping for product {product.id}: {e}")
                await db.commit()
                await append_cleaning_task_log(db, task_id, "Global name mapping applied to all products")
            else:
                await append_cleaning_task_log(db, task_id, "No global mapping created, proceeding with individual cleaning")
            await append_cleaning_task_log(db, task_id, "Pass 3: Cleaning individual attribute values...")
            updated_count = 0
            failed_count = 0
            for product in products:
                try:
                    product.enrichment_status = "processing"
                    product.updated_at = now_ist()
                    db.add(product)
                    await db.commit()
                    await db.refresh(product)
                    attr_stmt = (
                        select(Attribute.attribute_name, AttributeValue.value,
                               AttributeValue.uom, AttributeValue.id)
                        .join(AttributeValue, AttributeValue.attribute_id == Attribute.id)
                        .join(ProductAttributeValueLinkModel,
                              ProductAttributeValueLinkModel.attribute_value_id == AttributeValue.id)
                        .where(ProductAttributeValueLinkModel.product_id == product.id)
                    )
                    attr_result = await db.execute(attr_stmt)
                    attr_rows = attr_result.all()
                    if not attr_rows:
                        product.enrichment_status = "completed"
                        product.data_quality_score = 100.0
                        product.last_algorithm_used = llm_provider
                        product.updated_at = now_ist()
                        db.add(product)
                        await db.commit()
                        continue
                    attributes = []
                    for attr_idx, (attr_name, value, uom, av_id) in enumerate(attr_rows):
                        if value:
                            attributes.append(
                                AttributeInput(
                                    id=str(av_id),
                                    name=attr_name,
                                    value=value,
                                    unit=uom,
                                    source="existing_data",
                                )
                            )
                    if not attributes:
                        product.enrichment_status = "completed"
                        product.data_quality_score = 100.0
                        product.last_algorithm_used = llm_provider
                        product.updated_at = now_ist()
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
                        db, product.id, cleaning_result, llm_provider
                    )
                    product.enrichment_status = "completed"
                    product.data_quality_score = 100.0
                    product.last_algorithm_used = llm_provider
                    product.completeness_score = await _calculate_completeness(db, product)
                    product.updated_at = now_ist()
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
                        product.updated_at = now_ist()
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
                project = await db.get(Project, project_id)
                processing_status = project.status if project else 'completed'
                for source in sources:
                    new_metadata = dict(
                        source.source_metadata) if source.source_metadata else {}
                    new_metadata['processing_status'] = processing_status
                    source.source_metadata = new_metadata
                    flag_modified(source, "source_metadata")
                    db.add(source)
                await update_project_status(db, project_id)
                await db.commit()
            except Exception as status_error:
                logger.error(
                    f"Failed to update source/project status: {status_error}")
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
    timestamp = now_ist().isoformat()
    logs = task.logs or []
    logs.append(f"{timestamp} - {message}")
    task.logs = logs
    task.updated_at = now_ist()
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
        await update_project_status(db, request.project_id)
        await db.commit()
        task_id = str(uuid.uuid4())
        await create_cleaning_task(db, task_id)
        await db.commit()
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
        await db.rollback()
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
    llm_provider: str = "openai",
) -> bool:
    try:
        product = await db_session.get(Product, product_id)
        if not product:
            return False
        updated = False
        for ca in cleaning_response.cleaned_attributes:
            av_id = str(ca.id)
            attr_val = await db_session.get(AttributeValue, av_id)
            if not attr_val:
                continue
            old_value = str(attr_val.value or "")
            old_uom = attr_val.uom or ""
            final_val = str(ca.cleaned_value)
            final_unit = ca.unit or ""
            if final_unit and final_val.endswith(f" {final_unit}"):
                final_val = final_val.replace(f" {final_unit}", "").strip()
            name_changed = ca.name != ""
            val_changed = final_val != str(attr_val.value or "")
            unit_changed = final_unit != (attr_val.uom or "")
            link_stmt = select(ProjectProductLink).where(
                ProjectProductLink.product_id == product_id
            )
            link_result = await db_session.execute(link_stmt)
            link = link_result.scalars().first()
            project = await db_session.get(Project, link.project_id) if link else None
            if val_changed or unit_changed:
                edit_log = AttributeEditLog(
                    product_id=product_id,
                    project_id=link.project_id if link else None, 
                    catalog_project_name=project.name if project else None,
                    category_name=product.category_1 or product.taxonomy,
                    brand_name=product.brand_name,
                    product_name=product.product_name,
                    mpn=product.mpn,
                    attribute_name=ca.name,
                    old_value=old_value,
                    new_value=final_val,
                    algorithm_used=llm_provider,
                    edit_source='ai_cleaning'
                )
                db_session.add(edit_log)
                logger.info(
                    f"Updating attribute value: {attr_val.value} -> {final_val}")
                attr_val.value = final_val
                attr_val.uom = final_unit
                db_session.add(attr_val)
                updated = True
                if product.attributes is None:
                    product.attributes = {}
                product.attributes[ca.name] = {
                    "name": ca.name,
                    "value": final_val,
                    "unit": final_unit,
                    "sources": [llm_provider]
                }
                flag_modified(product, "attributes")
            if name_changed:
                attr_stmt = select(Attribute).where(
                    Attribute.id == attr_val.attribute_id)
                attr_result = await db_session.execute(attr_stmt)
                attribute = attr_result.scalars().first()
                if attribute and ca.name != attribute.attribute_name:
                    attribute.attribute_name = ca.name
                    db_session.add(attribute)
                    updated = True
        if updated:
            product.last_algorithm_used = llm_provider
            product.updated_at = now_ist()
            product.completeness_score = await _calculate_completeness(db_session, product)
            db_session.add(product)
        return updated
    except Exception as e:
        logger.error(f"Failed saving cleaned attributes: {e}", exc_info=True)
        raise
@router.get("/projects/{project_id}/download")
async def download_cleaned_project(
    project_id: str,
    db: AsyncSession = Depends(get_session)
):
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    stmt = select(Product).join(
        ProjectProductLink, Product.id == ProjectProductLink.product_id
    ).where(
        ProjectProductLink.project_id == project_id
    )
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
        try:
            val_stmt = (
                select(Attribute.attribute_name,
                       AttributeValue.value, AttributeValue.uom)
                .join(AttributeValue, AttributeValue.attribute_id == Attribute.id)
                .join(ProductAttributeValueLinkModel,
                      ProductAttributeValueLinkModel.attribute_value_id == AttributeValue.id)
                .where(ProductAttributeValueLinkModel.product_id == product.id)
                .limit(MAX_ATTRIBUTES)
            )
            val_result = await db.execute(val_stmt)
            for idx, (attr_name, value, uom) in enumerate(val_result.all()):
                i = idx + 1
                row[f"attribute_name{i}"] = attr_name or ""
                row[f"attribute_value{i}"] = value or ""
                row[f"attribute_uom{i}"] = uom or ""
        except Exception as e:
            logger.warning(f"Failed to read attributes for download: {e}")
        rows.append(row)
    df = pd.DataFrame(rows, columns=all_headers)
    excel_buffer = io.BytesIO()
    with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Cleaned Data')
    excel_buffer.seek(0)
    filename = f"Cleaned_Project_{project.name}_{now_ist().strftime('%Y%m%d_%H%M')}.xlsx"
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
        link_stmt = select(ProjectProductLink).where(
            ProjectProductLink.product_id == product.id
        )
        link_result = await db.execute(link_stmt)
        link = link_result.scalars().first()
        project_id = str(link.project_id) if link else None
        algorithm = (
            product.last_algorithm_used or product.used_llms[-1] if product.used_llms else 'Unknown')
        cleaning_service = LLMCleaningService(
            llm_provider=request.llm_provider or "openai",
            db=db,
            project_id=project_id
        )
        attr_stmt = (
            select(Attribute.attribute_name, AttributeValue)
            .join(AttributeValue, AttributeValue.attribute_id == Attribute.id)
            .join(ProductAttributeValueLinkModel,
                  ProductAttributeValueLinkModel.attribute_value_id == AttributeValue.id)
            .where(ProductAttributeValueLinkModel.product_id == product.id)
        )
        attr_result = await db.execute(attr_stmt)
        manual_edits = 0
        project = await db.get(Project, link.project_id) if link else None
        for attr_name, attr_val in attr_result.all():
            if attr_name in request.attributes:
                incoming = request.attributes[attr_name]
                old_value = str(attr_val.value or "")
                old_uom = attr_val.uom or ""
                attribute_input = AttributeInput(
                    id=str(attr_val.id),
                    name=attr_name,
                    value=incoming.value,
                    unit=incoming.uom or attr_val.uom or "",
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
                        attr_val.value = cleaned.cleaned_value
                        attr_val.uom = cleaned.unit or incoming.uom or ""
                    else:
                        attr_val.value = incoming.value
                        attr_val.uom = incoming.uom or ""
                except Exception as e:
                    logger.error(
                        f"Cleaning failed for attribute {attr_name}: {e}")
                    attr_val.value = incoming.value
                    attr_val.uom = incoming.uom or ""
                if product.attributes is None:
                    product.attributes = {}
                product.attributes[attr_name] = {
                    "name": attr_name,
                    "value": attr_val.value,
                    "unit": attr_val.uom,
                    "sources": ['bulk_manual_update']
                }
                flag_modified(product, 'attributes')
                new_value = str(attr_val.value or "")
                if old_value != new_value or old_uom != attr_val.uom:
                    edit_log = AttributeEditLog(
                        product_id=product_id,
                        project_id=link.project_id if link else None,
                        catalog_project_name=project.name if project else None,
                        category_name=product.category_1 or product.taxonomy,
                        brand_name=product.brand_name,
                        product_name=product.product_name,
                        mpn=product.product_code,
                        attribute_name=attr_name,
                        old_value=old_value,
                        new_value=new_value,
                        algorithm_used=algorithm,
                        edit_source='manual'
                    )
                    db.add(edit_log)
                    manual_edits += 1
                if (
                    product.validation_conflicts
                    and attr_name in product.validation_conflicts
                ):
                    del product.validation_conflicts[attr_name]
        if product.validation_conflicts:
            flag_modified(product, "validation_conflicts")
        product.manual_edit_count = (
            product.manual_edit_count or 0)+manual_edits
        total_populated = len(attr_result.all())
        if total_populated > 0:
            product.data_quality_score = max(
                0, 100-(product.manual_edit_count/total_populated)*100)
        product.completeness_score = await _calculate_completeness(db, product)
        product.updated_at = now_ist()
        db.add(product)
        await db.commit()
        return {"status": "success", "message": "Attributes updated"}
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to update attributes: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
async def _calculate_completeness(db: AsyncSession, product: Product) -> float:
    try:
        score = 0.0
        attr_stmt = (
            select(AttributeValue.value)
            .join(ProductAttributeValueLinkModel,
                  ProductAttributeValueLinkModel.attribute_value_id == AttributeValue.id)
            .where(ProductAttributeValueLinkModel.product_id == product.id)
        )
        attr_result = await db.execute(attr_stmt)
        attr_values = attr_result.scalars().all()
        if attr_values:
            filled = sum(1 for v in attr_values if v and str(v).strip())
            attr_score = (filled / len(attr_values)) * 60
            score += attr_score
        if product.image_url_1:
            score += 15
        if product.short_description and product.short_description.strip():
            score += 10
        if product.long_description and product.long_description.strip():
            score += 10
        if product.features and len(product.features) > 0:
            score += 5
        return round(score, 1)
    except Exception as e:
        logger.warning(
            f"Failed to calculate completeness for {product.product_code}: {e}")
        return product.completeness_score or 0.0
@router.put("/products/bulk-attributes")
async def bulk_update_product_attributes(
    request: BulkUpdateAttributesRequest,
    db: AsyncSession = Depends(get_session)
):
    try:
        if not request.product_ids:
            raise HTTPException(
                status_code=400, detail="No product IDs provided")
        if not request.attributes:
            raise HTTPException(
                status_code=400, detail="No attributes provided")
        stmt = select(Product).where(Product.id.in_(request.product_ids))
        result = await db.execute(stmt)
        products = result.scalars().all()
        if not products:
            raise HTTPException(status_code=404, detail="No products found")
        link_stmt = select(ProjectProductLink).where(
            ProjectProductLink.product_id == products[0].id
        )
        link_result = await db.execute(link_stmt)
        link = link_result.scalars().first()
        project_id = str(link.project_id) if link else None
        cleaning_service = LLMCleaningService(
            llm_provider=request.llm_provider or "openai",
            db=db,
            project_id=project_id
        )
        updated_count = 0
        for product in products:
            product_link_stmt = select(ProjectProductLink).where(
                ProjectProductLink.product_id == product.id
            )
            product_link_result = await db.execute(product_link_stmt)
            product_link = product_link_result.scalars().first()
            project = await db.get(Project, product_link.project_id) if product_link else None
            algorithm = request.llm_provider or product.last_algorithm_used or (
                product.used_llms[-1] if product.used_llms else 'Unknown')
            attr_stmt = (
                select(Attribute.attribute_name, AttributeValue)
                .join(AttributeValue, AttributeValue.attribute_id == Attribute.id)
                .join(ProductAttributeValueLinkModel,
                      ProductAttributeValueLinkModel.attribute_value_id == AttributeValue.id)
                .where(ProductAttributeValueLinkModel.product_id == product.id)
            )
            attr_result = await db.execute(attr_stmt)
            attr_rows = attr_result.all()
            if not attr_rows:
                continue
            updated = False
            manual_edits = 0
            context = ProductContext(
                mpn=product.product_code,
                brand=product.brand_name,
                product_name=product.product_name,
                taxonomy=product.taxonomy
            )
            for attr_name, attr_val in attr_rows:
                if attr_name not in request.attributes:
                    continue
                old_value = str(attr_val.value or "")
                old_uom = attr_val.uom or ""
                raw_value = request.attributes[attr_name]
                attribute_input = AttributeInput(
                    id=str(attr_val.id),
                    name=attr_name,
                    value=raw_value,
                    unit=attr_val.uom or "",
                    source="bulk_update"
                )
                try:
                    cleaning_result = await cleaning_service.clean_attributes(
                        [attribute_input],
                        context
                    )
                    if cleaning_result.cleaned_attributes:
                        cleaned = cleaning_result.cleaned_attributes[0]
                        attr_val.value = cleaned.cleaned_value
                        attr_val.uom = cleaned.unit or ""
                    else:
                        attr_val.value = raw_value
                except Exception as e:
                    logger.error(f"Bulk cleaning failed: {e}", exc_info=True)
                    attr_val.value = raw_value
                new_value = str(attr_val.value or "")
                if old_value != new_value or old_uom != attr_val.uom:
                    edit_log = AttributeEditLog(
                        product_id=product.id,
                        project_id=product_link.project_id if product_link else None,
                        catalog_project_name=project.name if project else None,
                        category_name=product.category_1 or product.taxonomy,
                        brand_name=product.brand_name,
                        product_name=product.product_name,
                        mpn=product.product_code,
                        attribute_name=attr_name,
                        old_value=old_value,
                        new_value=new_value,
                        algorithm_used=algorithm,
                        edit_source="manual",
                    )
                    db.add(edit_log)
                    manual_edits += 1
                db.add(attr_val)
                updated = True
            if updated:
                product.manual_edit_count = (
                    product.manual_edit_count or 0) + manual_edits
                total_populated = len(attr_rows)
                if total_populated > 0:
                    product.data_quality_score = max(
                        0, 100 - (product.manual_edit_count / total_populated) * 100)
                product.completeness_score = await _calculate_completeness(db, product)
                product.updated_at = now_ist()
                if product.validation_conflicts:
                    for attr_name in request.attributes.keys():
                        if attr_name in product.validation_conflicts:
                            del product.validation_conflicts[attr_name]
                    flag_modified(product, "validation_conflicts")
                product.updated_at = now_ist()
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
            raise HTTPException(
                status_code=400, detail="No products or projects selected")
        stmt = select(Product).join(
            ProjectProductLink, Product.id == ProjectProductLink.product_id, isouter=True
        )
        filters = []
        if request.product_ids:
            filters.append(Product.id.in_(request.product_ids))
        if request.project_ids:
            filters.append(ProjectProductLink.project_id.in_(
                request.project_ids))  
        stmt = stmt.where(or_(*filters))
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
