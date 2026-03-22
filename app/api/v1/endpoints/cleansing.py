from fastapi import APIRouter, Depends, HTTPException,BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from app.core.database import get_session
from app.models.pipeline import CleansingIssue
import logging
from sqlalchemy.orm.attributes import flag_modified
from app.aggregation.services.cleaning_service import AttributeInput, LLMCleaningResponse, LLMCleaningService, ProductContext
import uuid
from fastapi.responses import StreamingResponse
import pandas as pd
import io
logger = logging.getLogger("cleansing_router")
router = APIRouter()
from app.models.project import Project
from app.models.product import Product
from typing import Dict,List
task_status_store = {}
from datetime import datetime
@router.get("/issues")
async def get_all_issues(db: AsyncSession = Depends(get_session)):
    try:
        statement = select(CleansingIssue).order_by(CleansingIssue.detected_at.desc())
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
@router.post("/projects/{project_id}/clean")
async def clean_project(
    project_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session)
):
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    task_id = str(uuid.uuid4())
    task_status_store[task_id] = {
        "status": "pending",
        "logs": []  
    }
    background_tasks.add_task(run_project_cleaning, project_id, task_id, db)
    return {"status": "accepted", "task_id": task_id}
from datetime import datetime
def add_log(task_id: str, message: str):
    if task_id in task_status_store:
        timestamp = datetime.utcnow().isoformat()
        task_status_store[task_id]["logs"].append(f"{timestamp} - {message}")    
async def run_project_cleaning(project_id: str, task_id: str, db: AsyncSession):
    add_log(task_id, f"Starting cleaning for project {project_id}")
    task_status_store[task_id]["status"] = "running"
    service = LLMCleaningService(concurrency_limit=10)
    stmt = select(Product).where(Product.project_id == project_id)
    result = await db.execute(stmt)
    products = result.scalars().all()
    total = len(products)
    updated_count = 0
    add_log(task_id, f"Found {total} products to process")
    for idx, product in enumerate(products):
        logger.info(f"Processing product {product.product_code}")
        if not product.dynamic_attributes:
            add_log(task_id, f"  No dynamic attributes, skipping")
            continue
        attributes = []
        for attr_idx, attr in enumerate(product.dynamic_attributes):
            if attr.get('value'):
                attributes.append(AttributeInput(
                    id=str(attr_idx),
                    name=attr.get('name', ''),
                    value=attr.get('value', ''),
                    unit=attr.get('unit') or attr.get('uom'),
                    source="existing_data"
                ))
        if not attributes:
            add_log(task_id, f"  No attributes with values, skipping")
            continue
        context = ProductContext(
            mpn=product.product_code,
            brand=product.brand_name,
            product_name=product.product_name,
            taxonomy=product.taxonomy
        )
        try:
            add_log(task_id, f"  Calling LLM cleaning for {len(attributes)} attributes")
            cleaning_result = await service.clean_attributes(attributes, context)
            updated = await save_cleaned_attributes(db, product.id, cleaning_result, product.dynamic_attributes)
            if updated:
                logger.info(f"Product {product.product_code} updated in database")
                updated_count += 1
                add_log(task_id, f"  Product updated")
            else:
                logger.info(f"No changes for product {product.product_code}")
                add_log(task_id, f"  No changes needed")
            logger.info(f"Original attributes for {product.product_code}: {attributes}")
            logger.info(f"Cleaned response: {cleaning_result}")
        except Exception as e:
            add_log(task_id, f"  ERROR: {str(e)}")
            logger.error(f"Failed for product {product.product_code}: {e}")
    add_log(task_id, f"Cleaning completed. Updated {updated_count}/{total} products.")
    task_status_store[task_id]["status"] = "completed"
@router.get("/tasks/{task_id}")
async def get_task_status(task_id: str):
    task = task_status_store.get(task_id)
    if not task:
        raise HTTPException(404)
    return task
async def save_cleaned_attributes(
    db_session,
    product_id: str,
    cleaning_response: LLMCleaningResponse,
    original_attributes: List[Dict]
) -> bool:
    from app.models.product import Product
    product = await db_session.get(Product, product_id)
    if not product or not product.dynamic_attributes:
        return False
    cleaned_by_id = {str(ca.id): ca for ca in cleaning_response.cleaned_attributes}
    updated = False
    new_attrs = [dict(a) for a in product.dynamic_attributes]
    for idx, attr in enumerate(new_attrs):
        attr_id = str(idx)
        cleaned = cleaned_by_id.get(attr_id)
        if cleaned:
            name_changed = cleaned.name != attr.get('name')
            final_val = str(cleaned.cleaned_value)
            final_unit = cleaned.unit or ""
            if final_unit and final_val.endswith(f" {final_unit}"):
                final_val = final_val.replace(f" {final_unit}", "").strip()
            val_changed = final_val != str(attr.get('value'))
            unit_changed = final_unit != (attr.get('unit') or attr.get('uom'))
            if name_changed or val_changed or unit_changed:
                logger.info(f"Updating {attr['name']}: {attr.get('value')} -> {cleaned.cleaned_value}")
                attr['name']=cleaned.name   
                attr['value'] = final_val
                attr['unit'] = final_unit
                updated = True
    if updated:
        product.dynamic_attributes = new_attrs
        flag_modified(product, "dynamic_attributes") 
        product.updated_at = datetime.utcnow()
        db_session.add(product)
        await db_session.commit()
        await db_session.refresh(product)
        return True
    return False
@router.get("/tasks/{task_id}/logs")
async def get_task_logs(task_id: str):
    task = task_status_store.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"logs": task.get("logs", [])}
@router.get("/projects/{project_id}/download")
async def download_cleaned_project(
    project_id: str,
    db: AsyncSession = Depends(get_session)
):
    """Download cleaned products as an Excel file."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    stmt = select(Product).where(Product.project_id == project_id).order_by(Product.created_at.asc())
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
    all_headers = base_headers + media_headers + content_headers + attr_headers + source_url_headers
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
        row["Base Price"] = str(product.base_price) if product.base_price else ""
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
                row[f"attribute_uom{i}"] = attr.get("unit") or attr.get("uom") or ""
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