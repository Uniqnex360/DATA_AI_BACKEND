from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status, Form, UploadFile, File, Request, Query
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from sqlalchemy import func
from app.models.pipeline import AuditTrail, CleansingIssue, RawExtraction, Source, SourcePriority
from app.core.database import get_session, async_session_factory
from app.models.product import Product
from typing import List
import logging
import json
import io
import hashlib
from datetime import datetime,timedelta
from app.aggregation.aggregate_product import aggregate_product
from app.schemas.extraction import ExtractionRequest, SourceMetricsResponse, SourceResponse
from app.schemas.pipeline import SourcePriorityResponse
import pandas as pd
import os
from app.models.project import Project
from uuid import uuid4
from app.utils.parsers import infer_taxonomy_for_row, parse_import_file
from app.utils.matching import get_or_create_brand,get_or_create_vendor,get_or_create_industry
from app.utils.validators import is_invalid
logger = logging.getLogger("extraction_router")
router = APIRouter()
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
    
# def clean_for_excel(val, attr_name=None):
#     """Safely extracts just the value string, removing JSON keys."""
#     if val is None or val == "": return ""
#     if isinstance(val, list):
#         return " | ".join([clean_for_excel(i, attr_name) for i in val])
#     if isinstance(val, dict):
#         if "value" in val:
#             return str(val["value"])
#         if attr_name:
#             norm_attr = str(attr_name).lower().replace(" ", "").replace("_", "")
#             for k, v in val.items():
#                 if norm_attr in k.lower().replace("_", "") or k.lower().replace("_", "") in norm_attr:
#                     return clean_for_excel(v, attr_name)
#         return ", ".join([str(v) for v in val.values()])
#     return str(val)
def clean_for_excel(val, attr_name=None):
    """Extracts clean strings from AI JSON objects and removes redundant keys."""
    if val is None or val == "": return ""
    if isinstance(val, (str, int, float)): return str(val)
    if isinstance(val, dict):
        # Handle standard unit objects {"value": 150, "uom": "W"}
        if "value" in val and val["value"] is not None:
            return str(val["value"])
        # Unwrap if key matches attribute name {"color": "Red"} -> "Red"
        if attr_name:
            n = str(attr_name).lower().replace("_", "").replace(" ", "")
            for k, v in val.items():
                if k.lower().replace("_", "") == n: return clean_for_excel(v)
        # Fallback: Join values
        vals = [str(v) for v in val.values() if v is not None and not isinstance(v, (dict, list))]
        return vals[0] if len(vals) == 1 else ", ".join(vals)
    if isinstance(val, list):
        return " | ".join([clean_for_excel(i, attr_name) for i in val])
    return str(val)

def semantic_match_key(target_name: str, ai_keys: list) -> str:
    """Fuzzy matcher to link 'Color Temperature' to 'color_temp_k' etc."""
    import re
    from difflib import SequenceMatcher
    def cl(s): return re.sub(r'[^a-z0-9]', '', str(s).lower())
    target_clean = cl(target_name)
    best_key, highest_score = None, 0
    for key in ai_keys:
        key_clean = cl(key)
        score = 0
        if target_clean == key_clean: score = 100
        elif target_clean in key_clean or key_clean in target_clean:
            overlap = min(len(target_clean), len(key_clean)) / max(len(target_clean), len(key_clean))
            score = 80 + (overlap * 19)
        else:
            ratio = SequenceMatcher(None, target_clean, key_clean).ratio()
            if ratio > 0.8: score = 60 + (ratio * 20)
        if score > highest_score and score > 75:
            highest_score, best_key = score, key
    return best_key
# @router.get("/{source_id}/download")
# async def download_file(
#     source_id: str,
#     download_type: str = Query("input", alias="type"),
#     db: AsyncSession = Depends(get_session)
# ):
#     try:
#         source = await db.get(Source, source_id)
#         if not source:
#             raise HTTPException(
#                 status_code=404, detail="Source record not found")
#         if download_type == 'input':
#             if source.content_data:
#                 return StreamingResponse(
#                     io.BytesIO(source.content_data),
#                     media_type="application/octet-stream",
#                     headers={
#                         "Content-Disposition": f"attachment; filename=Input_{source.source_url}"}
#                 )
#             output = io.StringIO()
#             output.write(
#                 f"SKU: {source.source_url}\nUploaded: {source.uploaded_at}")
#             return StreamingResponse(io.BytesIO(output.getvalue().encode()), media_type="text/plain")
#         elif download_type == 'output':
#             stmt = select(Product).where(
#                 Product.project_id == source.project_id, 
#                 Product.source_url == source.source_url
#             )
#             result = await db.execute(stmt)
#             products = result.scalars().all()
#             if not products:
#                 raise HTTPException(status_code=404, detail="No enriched data found")
#             core_headers = ["Prod ID", "SKU", "Product_Type", "Parent_SKU", "Product_Name", "Brand", "GTIN", "ean", "upc", "unspc", "MPN", "Discontinue_Status"]
#             cat_headers = ["industry_name", "category 1", "category 2", "category 3", "category 4", "category 5", "category 6", "category 7", "category 8", "Taxonomy"]
#             phys_headers = ["Country_of_Origin", "Warranty", "Weight", "Weight_Unit", "Length", "Width", "Height", "Dimension_Unit", "Variant_Status"]
#             price_headers = ["Currency", "Base Price", "Sale Price", "Selling_Price", "Special_Price", "Stock_Qty", "Stock_Status", "Vendor_Name", "Vendor_SKU"]
#             media_headers = []
#             for i in range(1, 9): media_headers.extend([f"image_name_{i}", f"image_url_{i}"])
#             for i in range(1, 4): media_headers.extend([f"video_name_{i}", f"video_url_{i}"])
#             for i in range(1, 6): media_headers.extend([f"document_name_{i}", f"document_url_{i}"])
#             content_headers = ["3D_Model_URL", "Short_Description", "Long_Description", "features_1", "features_2", "features_3", "features_4", "features_5", "features_6", "features_7", "features_8", "features_9", "features_10", "Meta_Title", "Meta_Description", "Search_Keywords", "Certification", "Safety_Standard", "Hazardous_Material", "Prop65_Warning"]
#             attr_headers = []
#             for i in range(1, 21):
#                 attr_headers.extend([f"attribute_name{i}", f"attribute_value{i}", f"attribute_uom{i}", f"validation_value{i}", f"validation_uom{i}"])
#             all_headers = core_headers + cat_headers + phys_headers + price_headers + media_headers + content_headers + attr_headers
#             IGNORED_OUTPUT_KEYS = [
#                 "share", "latest_news", "search_for", "error_ref", "important", 
#                 "frequently_bought_together", "select_all", "contact_info", "customer_service", "phone", "email"
#             ]
#             export_rows = []
#             for p in products:
#                 row = {h: "" for h in all_headers}
#                 row.update({
#                     "SKU": p.sku, "Product_Name": p.product_name, "Brand": p.brand_name, "MPN": p.product_code,
#                     "GTIN": p.gtin, "upc": p.upc, "ean": p.ean, "unspc": p.unspc, "Taxonomy": p.taxonomy,
#                     "industry_name": p.industry_name, "category 1": p.category_1, "category 2": p.category_2,
#                     "Base Price": p.base_price, "Currency": p.currency, "Weight": p.weight, "Warranty": p.warranty,
#                     "Short_Description": p.short_description, "Long_Description": p.long_description, "Meta_Title": p.meta_title
#                 })
#                 ai_data = p.attributes or {}
#                 user_defined = p.dynamic_attributes or [] 
#                 def norm(s): return str(s).lower().replace(" ", "").replace("_", "").replace("-", "")
#                 used_ai_keys = set()
#                 for i in range(1, 6):
#                     target_name = user_defined[i-1].get("name", "") if len(user_defined) >= i else ""
#                     if not target_name: continue
#                     row[f"attribute_name{i}"] = target_name
#                     norm_target = norm(target_name)
#                     match_key = None
#                     for ai_key in ai_data.keys():
#                         if norm_target == norm(ai_key) or norm(ai_key) in norm_target or norm_target in norm(ai_key):
#                             match_key = ai_key
#                             break
#                     if match_key:
#                         val = ai_data[match_key]
#                         row[f"attribute_value{i}"] = clean_for_excel(val, target_name)
#                         if isinstance(val, dict) and "uom" in val:
#                             row[f"attribute_uom{i}"] = val["uom"]
#                         used_ai_keys.add(match_key)
#                 remaining_ai_keys = [
#                     k for k in ai_data.keys() 
#                     if k not in used_ai_keys and k.lower() not in IGNORED_OUTPUT_KEYS
#                 ]
#                 current_ai_idx = 0
#                 for i in range(1, 21):
#                     if not row[f"attribute_name{i}"] and current_ai_idx < len(remaining_ai_keys):
#                         key = remaining_ai_keys[current_ai_idx]
#                         val = ai_data[key]
#                         row[f"attribute_name{i}"] = key.replace('_', ' ').title()
#                         row[f"attribute_value{i}"] = clean_for_excel(val, key)
#                         if isinstance(val, dict) and "uom" in val:
#                             row[f"attribute_uom{i}"] = val["uom"]
#                         current_ai_idx += 1
#                 export_rows.append(row)
#             df = pd.DataFrame(export_rows, columns=all_headers)
#             excel_buffer = io.BytesIO()
#             with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
#                 df.to_excel(writer, index=False, sheet_name='Enriched Data')
#             excel_buffer.seek(0)
#             filename = f"Enriched_Data_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
#             return StreamingResponse(
#                 excel_buffer,
#                 media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
#                 headers={"Content-Disposition": f"attachment; filename={filename}"}
#             )
#     except Exception as e:
#         logger.error(f"Download Error: {str(e)}")
#         raise HTTPException(
#             status_code=500, detail="Error generating download")

@router.get("/{source_id}/download")
async def download_file(
    source_id: str,
    download_type: str = Query("input", alias="type"),
    db: AsyncSession = Depends(get_session)
):
    try:
        source = await db.get(Source, source_id)
        if not source:
            raise HTTPException(status_code=404, detail="Source record not found")

        # ═══════════════════════════════════════════════════════════
        # INPUT DOWNLOAD (unchanged - working fine)
        # ═══════════════════════════════════════════════════════════
        if download_type == 'input':
            if source.content_data:
                return StreamingResponse(
                    io.BytesIO(source.content_data),
                    media_type="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename=Input_{source.source_url}"}
                )
            output = io.StringIO()
            output.write(f"SKU: {source.source_url}\nUploaded: {source.uploaded_at}")
            return StreamingResponse(io.BytesIO(output.getvalue().encode()), media_type="text/plain")

        # ═══════════════════════════════════════════════════════════
        # OUTPUT DOWNLOAD (FIXED)
        # ═══════════════════════════════════════════════════════════
        elif download_type == 'output':
            stmt = select(Product).where(
                Product.project_id == source.project_id,
                Product.source_url == source.source_url
            )
            result = await db.execute(stmt)
            products = result.scalars().all()
            
            if not products:
                raise HTTPException(status_code=404, detail="No enriched data found")

            # ✅ DEFINE ALL 180 COLUMN HEADERS (EXACT MATCH TO INPUT)
            core_headers = [
                "Prod ID", "SKU", "Product_Type", "Parent_SKU", "Product_Name", 
                "Brand", "GTIN", "ean", "upc", "unspc", "MPN", "Status", 
                "Lifecycle_Stage", "Launch_Date", "Discontinue_Status"
            ]
            
            cat_headers = [
                "industry_name", "category 1", "category 2", "category 3", 
                "category 4", "category 5", "category 6", "category 7", 
                "category 8", "Taxonomy"
            ]
            
            phys_headers = [
                "Country_of_Origin", "Warranty", "Weight", "Weight_Unit", 
                "Length", "Width", "Height", "Dimension_Unit", "Variant_Status"
            ]
            
            price_headers = [
                "Currency", "Base Price", "Sale Price", "Selling_Price", 
                "Special_Price", "Stock_Qty", "Stock_Status", "Vendor_Name", "Vendor_SKU"
            ]
            
            # Media headers (8 images, 3 videos, 5 documents)
            media_headers = []
            for i in range(1, 9):
                media_headers.extend([f"image_name_{i}", f"image_url_{i}"])
            for i in range(1, 4):
                media_headers.extend([f"video_name_{i}", f"video_url_{i}"])
            for i in range(1, 6):
                media_headers.extend([f"document_name_{i}", f"document_url_{i}"])
            
            content_headers = [
                "3D_Model_URL", "Short_Description", "Long_Description",
                "features_1", "features_2", "features_3", "features_4", "features_5",
                "features_6", "features_7", "features_8", "features_9", "features_10",
                "Meta_Title", "Meta_Description", "Search_Keywords",
                "Certification", "Safety_Standard", "Hazardous_Material", "Prop65_Warning"
            ]
            
            # 20 attribute slots with 5 fields each
            attr_headers = []
            for i in range(1, 21):
                attr_headers.extend([
                    f"attribute_name{i}", f"attribute_value{i}", f"attribute_uom{i}",
                    f"validation_value{i}", f"validation_uom{i}"
                ])
            
            all_headers = core_headers + cat_headers + phys_headers + price_headers + media_headers + content_headers + attr_headers

            # ✅ IGNORED KEYS (non-product attributes from AI)
            IGNORED_OUTPUT_KEYS = {
                "share", "latest_news", "search_for", "error_ref", "important",
                "frequently_bought_together", "select_all", "contact_info",
                "customer_service", "phone", "email", "hours", "best_sellers_rank",
                "asin", "date_first_available", "customer_reviews", "return_policy"
            }

            export_rows = []
            
            for p in products:
                # Initialize row with all headers
                row = {h: "" for h in all_headers}
                
                # ✅ FILL CORE FIELDS
                row.update({
                    "Prod ID": p.id or "",
                    "SKU": p.sku or "",
                    "Product_Type": p.product_type or "",
                    "Parent_SKU": p.parent_sku or "",
                    "Product_Name": p.product_name or "",
                    "Brand": p.brand_name or "",
                    "GTIN": p.gtin or "",
                    "ean": p.ean or "",
                    "upc": p.upc or "",
                    "unspc": p.unspc or "",
                    "MPN": p.product_code or "",
                    "Status": getattr(p, 'status', ''),
                    "Lifecycle_Stage": p.lifecycle_stage or "",
                    "Launch_Date": p.launch_date or "",
                    "Discontinue_Status": p.discontinue_status or "",
                })
                
                # ✅ FILL CATEGORY FIELDS
                row.update({
                    "industry_name": p.industry_name or "",
                    "category 1": p.category_1 or "",
                    "category 2": p.category_2 or "",
                    "category 3": p.category_3 or "",
                    "category 4": p.category_4 or "",
                    "category 5": p.category_5 or "",
                    "category 6": p.category_6 or "",
                    "category 7": p.category_7 or "",
                    "category 8": p.category_8 or "",
                    "Taxonomy": p.taxonomy or "",
                })
                
                # ✅ FILL PHYSICAL FIELDS
                row.update({
                    "Country_of_Origin": getattr(p, 'country_of_origin', ''),
                    "Warranty": p.warranty or "",
                    "Weight": p.weight or "",
                    "Weight_Unit": p.weight_unit or "",
                    "Length": getattr(p, 'length', ''),
                    "Width": getattr(p, 'width', ''),
                    "Height": getattr(p, 'height', ''),
                    "Dimension_Unit": getattr(p, 'dimension_unit', ''),
                    "Variant_Status": getattr(p, 'variant_status', ''),
                })
                
                # ✅ FILL PRICING FIELDS
                row.update({
                    "Currency": p.currency or "",
                    "Base Price": p.base_price or "",
                    "Sale Price": getattr(p, 'sale_price', ''),
                    "Selling_Price": getattr(p, 'selling_price', ''),
                    "Special_Price": getattr(p, 'special_price', ''),
                    "Stock_Qty": getattr(p, 'stock_qty', ''),
                    "Stock_Status": getattr(p, 'stock_status', ''),
                    "Vendor_Name": p.vendor_name or "",
                    "Vendor_SKU": getattr(p, 'vendor_sku', ''),
                })
                
                # ✅ FILL MEDIA FIELDS (if stored in product.images, product.videos, product.documents)
                if hasattr(p, 'images') and p.images:
                    for idx, (key, img) in enumerate(list(p.images.items())[:8], 1):
                        row[f"image_name_{idx}"] = img.get('name', '')
                        row[f"image_url_{idx}"] = img.get('url', '')
                
                # ✅ FILL CONTENT FIELDS
                row.update({
                    "Short_Description": p.short_description or "",
                    "Long_Description": p.long_description or "",
                    "Meta_Title": p.meta_title or "",
                    "Meta_Description": getattr(p, 'meta_description', ''),
                    "Search_Keywords": getattr(p, 'search_keywords', ''),
                })
                
                # ✅ FILL FEATURES (if stored as list)
                if hasattr(p, 'features') and p.features:
                    for idx, feature in enumerate(p.features[:10], 1):
                        row[f"features_{idx}"] = feature

                # ═══════════════════════════════════════════════════════════
                # ✅ CRITICAL: DYNAMIC ATTRIBUTES (FIXED LOGIC)
                # ═══════════════════════════════════════════════════════════
                
                ai_data = p.attributes or {}
                user_defined = p.dynamic_attributes or [] 
                used_ai_keys = set()

                # --- PHASE 1: Fill Priority Slots 1-5 (User-defined names from CSV) ---
                for i in range(1, 6):
                    target_name = user_defined[i-1].get("name", "").strip() if len(user_defined) >= i else ""
                    if not target_name: continue

                    row[f"attribute_name{i}"] = target_name
                    
                    # Search AI results for a semantic match (e.g. "Watts" -> "wattage")
                    match_key = semantic_match_key(target_name, list(ai_data.keys()))
                    
                    if match_key:
                        val_obj = ai_data[match_key]
                        row[f"attribute_value{i}"] = clean_for_excel(val_obj, target_name)
                        # Extract UOM if AI returned it in a structured dict
                        if isinstance(val_obj, dict) and "uom" in val_obj:
                            row[f"attribute_uom{i}"] = val_obj["uom"]
                        used_ai_keys.add(match_key)

                # --- PHASE 2: Fill ALL empty slots with Discovered AI data ---
                remaining_ai_keys = [
                    k for k in ai_data.keys() 
                    if k not in used_ai_keys and k.lower() not in IGNORED_OUTPUT_KEYS
                ]
                
                current_ai_idx = 0
                for i in range(1, 21):
                    # If slot is empty (no priority name or match failed), fill it with a discovery
                    if not row[f"attribute_name{i}"] and current_ai_idx < len(remaining_ai_keys):
                        key = remaining_ai_keys[current_ai_idx]
                        val_obj = ai_data[key]
                        
                        row[f"attribute_name{i}"] = key.replace('_', ' ').title()
                        row[f"attribute_value{i}"] = clean_for_excel(val_obj, key)
                        if isinstance(val_obj, dict) and "uom" in val_obj:
                            row[f"attribute_uom{i}"] = val_obj["uom"]
                            
                        current_ai_idx += 1

                export_rows.append(row)

            # ✅ CREATE EXCEL
            df = pd.DataFrame(export_rows, columns=all_headers)
            
            excel_buffer = io.BytesIO()
            with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                df.to_excel(writer, index=False, sheet_name='Enriched Data')
            
            excel_buffer.seek(0)
            
            filename = f"Enriched_Data_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
            
            return StreamingResponse(
                excel_buffer,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f"attachment; filename={filename}"}
            )

    except Exception as e:
        logger.error(f"Download Error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error generating download")

ALLOWED_EXTENSIONS = {'.csv', '.xlsx', '.xls'}
MAX_FILE_SIZE = 10 * 1024 * 1024
MAX_ROWS = 1000
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
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Project ID is required.")
        project = await db.get(Project, projectId)
        if not project:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Project {projectId} not found")
        file_ext = os.path.splitext(file.filename)[1].lower()
        if file_ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid file type.")
        content = bytearray()
        chunk_size = 1024 * 1024
        while True:
            chunk = await file.read(chunk_size)
            if not chunk: break
            content.extend(chunk)
            if len(content) > MAX_FILE_SIZE:
                raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File too large")
        content = bytes(content)
        file_hash = hashlib.sha256(content).hexdigest()
        recent_cutoff = datetime.utcnow() - timedelta(hours=24)
        duplicate_check = select(Source).where(
            Source.project_id == projectId,
            func.json_extract_path_text(Source.source_metadata, 'file_hash') == file_hash,
            Source.created_at > recent_cutoff
        )
        if await db.scalar(duplicate_check):
            raise HTTPException(status.HTTP_409_CONFLICT, "File already uploaded recently.")
        rows = parse_import_file(content, file.filename)
        total_rows = len(rows)
        if total_rows == 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "File is empty or invalid format")
        if total_rows > MAX_ROWS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Too many rows ({total_rows}). Max {MAX_ROWS}.")
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
            source_metadata={
                "file_hash": file_hash,
                "total": total_rows,
                "inferred_taxonomies": inferred_count,
                "aggregation_status": "pending"
            }
        )
        db.add(new_source)
        await db.commit()
        await db.refresh(new_source)
        created_count = 0
        updated_count = 0
        for idx, row in enumerate(rows):
            code = row.get("mpn") or row.get("sku") or f"UNK-{uuid4()}"
            stmt = select(Product).where(Product.product_code == str(code))
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
                    brand.country_of_origin=row.get('country_of_origin')
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
                    product.base_price = float(str(row["base_price"]).replace(',', ''))
                    product.currency = row.get("currency", "USD")
            except ValueError:
                pass
            product.enrichment_status = "pending"
            db.add(product)
        await db.commit()
        logger.info(f"Batch import saved: {created_count} new, {updated_count} updated. Waiting for manual aggregation.")
        return {
            "status": "accepted",
            "batch_id": str(new_source.id),
            "message": f"Imported {total_rows} products. Ready for aggregation."
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
                "aggregation_status": "pending"
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
            'aggregation_status': 'processing'
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
        
async def run_aggregation_task(source_id: str):
    async with async_session_factory() as db_session:
        try:
            source = await db_session.get(Source, source_id)
            if not source:
                return
            stmt = select(Product).where(
                Product.project_id == source.project_id,
                Product.enrichment_status == 'pending' 
            )
            result = await db_session.execute(stmt)
            products = result.scalars().all()
            successful = 0
            failed = 0
            total = len(products)
            logger.info(f"Starting aggregation task for source {source_id}, found {total} pending products.")
            for product in products:
                # 🧪 DEBUG LOG 1: Check what is actually in the DB
                logger.info(f"🔍 DB CHECK [{product.product_code}]: Taxonomy='{product.taxonomy}', Attrs={product.dynamic_attributes}")

                primary_attr_names = []
                if product.dynamic_attributes:
                    primary_attr_names = [
                        attr['name'] for attr in product.dynamic_attributes 
                        if isinstance(attr, dict) and attr.get('name')
                    ]
                
                # 🧪 DEBUG LOG 2: Check if extraction worked
                logger.info(f"🎯 PRIORITY LIST: {primary_attr_names}")

            
            for idx, product in enumerate(products):
                try:
                    logger.info(f"Aggregating {idx+1}/{total}: {product.product_code}")
                    primary_attr_names = []
                    if product.dynamic_attributes:
                        primary_attr_names = [
                            attr['name'] for attr in product.dynamic_attributes 
                            if isinstance(attr, dict) and attr.get('name')
                        ]
                    logger.info(f"Primary attributes found in DB: {primary_attr_names}")
                    
                    aggregation_result = await aggregate_product(
                        mpn=product.product_code,
                        title=product.product_name,
                        brand=product.brand_name,
                        taxonomy=product.taxonomy,       
                        primary_attributes=primary_attr_names,
                        db=db_session
                    )
                    if aggregation_result.get('status') == 'success':
                        ai_data = aggregation_result.get('golden_record', {}).get('attributes', {})
                        product.attributes = {**product.attributes, **ai_data}
                        product.enrichment_status = 'completed'
                        product.completeness_score = min(len(ai_data) * 5, 100)
                        db_session.add(RawExtraction(
                            source_id=source.id,
                            product_keys={"sku": product.product_code, "mpn": product.mpn},
                            raw_attributes=ai_data,
                            confidence=aggregation_result.get('golden_record', {}).get('confidence', 0.9),
                            extracted_at=datetime.utcnow()
                        ))
                        db_session.add(product)
                        successful += 1
                    else:
                        failed += 1
                        product.enrichment_status = 'failed'
                        db_session.add(product)
                except Exception as e:
                    logger.error(f"Aggregation loop error for {product.product_code}: {e}")
                    failed += 1
                    continue
            meta = dict(source.metadata or {})
            meta.update({
                "aggregation_status": "completed",
                "aggregated_successful": successful,
                "aggregated_failed": failed,
                "last_aggregation_time": datetime.utcnow().isoformat()
            })
            source.metadata = meta
            db_session.add(source)
            await db_session.commit()
            logger.info(f"Aggregation complete: {successful}/{total} successful, {failed} failed")
        except Exception as e:
            await db_session.rollback()
            logger.error(f"Aggregation task failed for {source_id}: {str(e)}")
