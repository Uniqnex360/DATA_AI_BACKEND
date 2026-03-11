from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status, Form, UploadFile, File, Request, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from sqlalchemy import func
from app.models.pipeline import AuditTrail, CleansingIssue, RawExtraction, Source, SourcePriority
from app.core.database import get_session, async_session_factory
from app.models.product import Product
from typing import List
import logging
from sqlalchemy.orm.attributes import flag_modified
from app.utils.usecase_validator import validate_file_against_use_case
import json
import io
import hashlib
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
from app.utils.validators import is_invalid
from app.utils.sanitize import sanitize_ai_data
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
    """
    Merge existing and AI-discovered attributes while preserving original attribute order.
    Preserves complete attribute structure including UOM/unit information.
    
    Args:
        primary_attributes: Original ordered list of attribute names
        existing_attrs: Dict of existing attribute values (may include {value, uom} structure)
        ai_data: Dict of newly discovered attributes from AI (may be plain or dict values)
    
    Returns:
        Merged dict with attributes in their original order, followed by new discoveries.
        Preserves value+uom structure where available.
    """
    merged = {}
    
    # First pass: add primary attributes in their original order
    for attr_name in primary_attributes:
        if attr_name in existing_attrs:
            # Keep existing value with full structure (value + uom)
            existing_val = existing_attrs[attr_name]
            # Preserve as-is if it's a dict, otherwise keep the value
            merged[attr_name] = existing_val if isinstance(existing_val, dict) else existing_val
        elif attr_name in ai_data:
            # Use AI value for missing primary attribute
            merged[attr_name] = ai_data[attr_name]
    
    # Second pass: add any new AI-discovered attributes not in primary list
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


# def semantic_match_key(target_name: str, ai_keys: list) -> str:
#     import re
#     from difflib import SequenceMatcher
#     def cl(s): return re.sub(r'[^a-z0-9]', '', str(s).lower())
#     target_clean = cl(target_name)
#     best_key, highest_score = None, 0
#     for key in ai_keys:
#         key_clean = cl(key)
#         score = 0
#         if target_clean == key_clean:
#             score = 100
#         elif target_clean in key_clean or key_clean in target_clean:
#             overlap = min(len(target_clean), len(key_clean)) / \
#                 max(len(target_clean), len(key_clean))
#             score = 80 + (overlap * 19)
#         else:
#             ratio = SequenceMatcher(None, target_clean, key_clean).ratio()
#             if ratio > 0.8:
#                 score = 60 + (ratio * 20)
#         if score > highest_score and score > 75:
#             highest_score, best_key = score, key
#     return best_key


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
                return StreamingResponse(
                    io.BytesIO(source.content_data),
                    media_type="application/octet-stream",
                    headers={
                        "Content-Disposition": f"attachment; filename=Input_{source.source_url}"}
                )
            return StreamingResponse(io.BytesIO(b"No data available"), media_type="text/plain")
        elif download_type == 'output':
            stmt = select(Product).where(
    Product.project_id == source.project_id,
    Product.source_url == source.source_url
).order_by(Product.created_at.asc())
            result = await db.execute(stmt)
            products = result.scalars().all()
            if not products:
                raise HTTPException(
                    status_code=404, detail="No enriched data found")
            project = await db.get(Project, products[0].project_id) if products else None
            use_case_lower = (project.use_case or "").lower() if project else ""
            if 'back filling' in use_case_lower or 'validation' in use_case_lower:
                MAX_ATTRIBUTES=40
            else:
                MAX_ATTRIBUTES=20
            logger.info(f"Using {MAX_ATTRIBUTES} attribute columns for use case: {project.use_case if project else 'Unknown'}")
            
            core_headers = ["Prod ID", "SKU", "Product_Type", "Parent_SKU", "Product_Name", "Brand", "GTIN",
                            "ean", "upc", "unspc", "MPN", "Status", "Lifecycle_Stage", "Launch_Date", "Discontinue_Status"]
            cat_headers = ["industry_name", "category 1", "category 2", "category 3",
                           "category 4", "category 5", "category 6", "category 7", "category 8", "Taxonomy"]
            phys_headers = ["Country_of_Origin", "Warranty", "Weight", "Weight_Unit",
                            "Length", "Width", "Height", "Dimension_Unit", "Variant_Status"]
            price_headers = ["Currency", "Base Price", "Sale Price", "Selling_Price",
                             "Special_Price", "Stock_Qty", "Stock_Status", "Vendor_Name", "Vendor_SKU"]
            media_headers = []
            for i in range(1, 9):
                media_headers.extend([f"image_name_{i}", f"image_url_{i}"])
            for i in range(1, 4):
                media_headers.extend([f"video_name_{i}", f"video_url_{i}"])
            for i in range(1, 6):
                media_headers.extend(
                    [f"document_name_{i}", f"document_url_{i}"])
            content_headers = ["3D_Model_URL", "Short_Description", "Long_Description",
                               "features_1", "features_2", "features_3", "features_4", "features_5",
                               "features_6", "features_7", "features_8", "features_9", "features_10",
                               "Meta_Title", "Meta_Description", "Search_Keywords",
                               "Certification", "Safety_Standard", "Hazardous_Material", "Prop65_Warning"]
            attr_headers = []
            for i in range(1, MAX_ATTRIBUTES + 1):
                attr_headers.extend([
                    f"attribute_name{i}", 
                    f"attribute_value{i}",
                    f"attribute_uom{i}", 
                    f"validation_value{i}", 
                    f"validation_uom{i}"
                ])
            source_url_headers = [f"source_url_{i}" for i in range(1, 6)]
            all_headers = core_headers + cat_headers + phys_headers + price_headers + \
                media_headers + content_headers + attr_headers+source_url_headers
            DEDICATED_COLUMN_MAPPING = {
                "name": "Product_Name",
                "product_name": "Product_Name",
                "title": "Product_Name",
                "brand": "Brand",
                "manufacturer": "Brand",
                "sku": "SKU",
                "mpn": "MPN",
                "model": "MPN",
                "product_type": "Product_Type",
                "type": "Product_Type",
                "parent_sku": "Parent_SKU",
                "gtin": "GTIN",
                "gtin13": "ean",
                "gtin12": "upc",
                "ean": "ean",
                "upc": "upc",
                "unspc": "unspc",
                "status": "Status",
                "lifecycle_stage": "Lifecycle_Stage",
                "launch_date": "Launch_Date",
                "discontinue_status": "Discontinue_Status",
                "weight": "Weight",
                "weight_unit": "Weight_Unit",
                "length": "Length",
                "width": "Width",
                "height": "Height",
                "dimension_unit": "Dimension_Unit",
                "dimensions": "Length",
                "country_of_origin": "Country_of_Origin",
                "made_in": "Country_of_Origin",
                "warranty": "Warranty",
                "warranty_period": "Warranty",
                "price": "Base Price",
                "base_price": "Base Price",
                "list_price": "Base Price",
                "sale_price": "Sale Price",
                "selling_price": "Selling_Price",
                "special_price": "Special_Price",
                "currency": "Currency",
                "stock": "Stock_Qty",
                "stock_qty": "Stock_Qty",
                "quantity": "Stock_Qty",
                "stock_status": "Stock_Status",
                "availability": "Stock_Status",
                "vendor": "Vendor_Name",
                "vendor_name": "Vendor_Name",
                "supplier": "Vendor_Name",
                "vendor_sku": "Vendor_SKU",
                "description": "Short_Description",
                "short_description": "Short_Description",
                "product_description": "Short_Description",
                "long_description": "Long_Description",
                "detailed_description": "Long_Description",
                "product_summary": "Short_Description",
                "meta_title": "Meta_Title",
                "meta_description": "Meta_Description",
                "keywords": "Search_Keywords",
                "search_keywords": "Search_Keywords",
                "seo_keywords": "Search_Keywords",
                "certification": "Certification",
                "certifications": "Certification",
                "safety_standard": "Safety_Standard",
                "safety_standards": "Safety_Standard",
                "hazardous": "Hazardous_Material",
                "hazardous_material": "Hazardous_Material",
                "prop65": "Prop65_Warning",
                "prop65_warning": "Prop65_Warning",
                "image": "image_url_1",
                "image_url": "image_url_1",
                "main_image": "image_url_1",
                "3d_model": "3D_Model_URL",
                "model_3d": "3D_Model_URL",
                # "images": "image_url_1"
            }
            IGNORED_KEYS = {
            "share", "latest_news", "search_for", "error_ref", "important",
            "frequently_bought_together", "select_all", "contact_info",
            "customer_service", "phone", "email", "hours", "best_sellers_rank",
            "asin", "date_first_available", "customer_reviews", "return_policy",
            "availability", "sold_by", "ships_from", "seller", "rating",
            "review_count", "reviews", "item_type", "catalog_number",
            "authentication_state", "location", "item_package_quantity",
            "color_options", "color_variants", "gtin14", "min_qty", "shipping_times", 
            "freight_extra", "contact_email", "contact_phone", "depth", "toll_free", 
            "case_pack", "original_price", "barcode", "pattern_run_time", 
            "shell_material", "lens_material", "flashes_per_minute", "wattage", 
            "power_source", "operating_life", "operating_temp", "number_of_leds", 
            "diameter", "additional_certifications", "alloy_range", "applications", 
            "blog", "certification_options", "chemical_physical_certifications",
            "compliance_specifications", "custom_products", "distribution",
            "established", "establishment", "finish", "hardness", "inspection_standards",
            "item_name", "item_number", "main_products", "manufactured_products",
            "chemical_and_physical_certifications", "compliance", "compliance_certification",
            "contact_information", "fax", "follow_us", "inspection_testing",
            "manufacturing_location", "material_traceability", "quality_management_certification",
            "quality_management_system", "url", "warning", "packaging_information",
            "baton_led_road_flares", "baton_road_flares_features", 
            "battery_operated_led_road_flares_features", "flex_fit_tripods",
            "led_flares_vs_incendiary_flares", "led_road_flares", "patterns_and_run_times",
            "price_range", "usage", "voc_level"
}
            def normalize_attr_name(s):
                """Normalize attribute names for consistent comparison."""
                return s.strip().lower().replace('_', '').replace(' ', '').replace('-', '')
            
            taxonomy_raw_data = {}
            for p in products:
                tax = p.taxonomy or "Unknown"
                if tax not in taxonomy_raw_data:
                    taxonomy_raw_data[tax] = {
                        'user_defined': [],
                        'user_defined_map': {},  # normalized -> original name
                        'ai_discovered': set(),
                    }
                data = taxonomy_raw_data[tax]
                if p.dynamic_attributes:
                    for attr in p.dynamic_attributes:
                        if isinstance(attr, dict) and attr.get('name'):
                            name = attr['name'].strip()
                            name_norm = normalize_attr_name(name)
                            if name and name_norm not in data['user_defined_map']:
                                # Store both the original name and normalized version
                                data['user_defined'].append(name)
                                data['user_defined_map'][name_norm] = name
                if p.attributes:
                    for key in p.attributes.keys():
                        key_lower = key.lower().strip()
                        if key_lower in IGNORED_KEYS:
                            continue
                        key_norm = key_lower.replace(
                            '_', '').replace(' ', '').replace('-', '')
                        is_dedicated = False
                        for map_key in DEDICATED_COLUMN_MAPPING.keys():
                            map_norm = map_key.lower().replace('_', '').replace(' ', '').replace('-', '')
                            if key_norm == map_norm:
                                is_dedicated = True
                                break
                        if not is_dedicated:
                            data['ai_discovered'].add(key)
            taxonomy_templates = {}
            for tax, data in taxonomy_raw_data.items():
                final_template = []
                added_normalized = set()

                def add_if_unique(name):
                    norm = normalize_attr_name(name)
                    if norm not in added_normalized:
                        added_normalized.add(norm)
                        final_template.append(name)
                        return True
                    return False
                
                for attr in data['user_defined']:
                    add_if_unique(attr)
                logger.info(f"📊 {tax}: Added {len(data['user_defined'])} Excel attributes in order")
                if tax != "Unknown":
                    category_attrs = await get_taxonomy_attribute_hints(tax, db)
                    for cat_attr in category_attrs:
                        add_if_unique(cat_attr)
                for ai_attr in sorted(data['ai_discovered']):
                    add_if_unique(ai_attr)
                taxonomy_templates[tax] = final_template[:MAX_ATTRIBUTES]
                logger.info(f" Unified template for '{tax}': {len(taxonomy_templates[tax])} attributes")
                logger.info(f"   User-defined: {len(data['user_defined'])}, AI-discovered: {len(data['ai_discovered'])}")
            export_rows = []
            project = await db.get(Project, products[0].project_id) if products else None
            is_validation_mode = False
            if project and project.use_case:
                is_validation_mode = 'validation' in project.use_case.lower()
                logger.info(f"Download mode: {'validation' if is_validation_mode else 'standard'}")
            for p in products:
                row = {h: "" for h in all_headers}
                ai_data = dict(p.attributes or {})
                taxonomy = p.taxonomy or "Unknown"
                attribute_template = taxonomy_templates.get(taxonomy, [])
                if 'images' in ai_data and isinstance(ai_data['images'], list):
                    images_list = ai_data.pop('images')
                    for i, img_url in enumerate(images_list[:8], 1):
                        row[f'image_url_{i}'] = clean_for_excel(img_url)
                    logger.info(
                        f"Mapped {len(images_list)} URLs from 'images' key.")
                elif 'image_url' in ai_data:
                    row['image_url_1'] = clean_for_excel(
                        ai_data.pop('image_url'))
                elif 'main_image' in ai_data:
                    row['image_url_1'] = clean_for_excel(
                        ai_data.pop('main_image'))
                elif 'image' in ai_data:
                    row['image_url_1'] = clean_for_excel(ai_data.pop('image'))
                if not row.get("image_url_1") and p.image_url_1:
                    row["image_url_1"] = p.image_url_1

                for ai_key in list(ai_data.keys()):
                    ai_key_norm = ai_key.lower().replace('_', '').replace(' ', '').replace('-', '')
                    for map_key, target_col in DEDICATED_COLUMN_MAPPING.items():
                        map_key_norm = map_key.lower().replace(
                            '_', '').replace(' ', '').replace('-', '')
                        if ai_key_norm == map_key_norm:
                            value = ai_data.pop(ai_key)
                            row[target_col] = clean_for_excel(value)
                            logger.info(
                                f"Mapped AI '{ai_key}' → Column '{target_col}'")
                            if isinstance(value, dict):
                                uom = value.get('uom') or value.get('unit')
                                if uom:
                                    uom_clean = clean_for_excel(uom)
                                    if target_col == 'Weight':
                                        row['Weight_Unit'] = uom_clean
                                    elif target_col in ['Length', 'Width', 'Height']:
                                        if not row['Dimension_Unit']:
                                            row['Dimension_Unit'] = uom_clean
                            break
                row.update({
                    "Prod ID": str(p.id) if p.id else "",
                    "SKU": row.get("SKU") or p.sku or "",
                    "Product_Type": row.get("Product_Type") or getattr(p, 'product_type', '') or "",
                    "Parent_SKU": row.get("Parent_SKU") or getattr(p, 'parent_sku', '') or "",
                    "Product_Name": row.get("Product_Name") or p.product_name or "",
                    "Brand": row.get("Brand") or p.brand_name or "",
                    "GTIN": row.get("GTIN") or getattr(p, 'gtin', '') or "",
                    "ean": row.get("ean") or getattr(p, 'ean', '') or "",
                    "upc": row.get("upc") or getattr(p, 'upc', '') or "",
                    "unspc": row.get("unspc") or getattr(p, 'unspc', '') or "",
                    "MPN": row.get("MPN") or p.product_code or "",
                    "Status": row.get("Status") or getattr(p, 'status', '') or "",
                    "Lifecycle_Stage": row.get("Lifecycle_Stage") or getattr(p, 'lifecycle_stage', '') or "",
                    "Launch_Date": row.get("Launch_Date") or getattr(p, 'launch_date', '') or "",
                    "Discontinue_Status": row.get("Discontinue_Status") or getattr(p, 'discontinue_status', '') or "",
                    "industry_name": p.industry_name or "",
                    "category 1": p.category_1 or "",
                    "category 2": p.category_2 or "",
                    "category 3": p.category_3 or "",
                    "category 4": p.category_4 or "",
                    "category 5": p.category_5 or "",
                    "category 6": p.category_6 or "",
                    "category 7": p.category_7 or "",
                    "category 8": p.category_8 or "",
                    "Taxonomy": taxonomy,
                    "Country_of_Origin": row.get("Country_of_Origin") or getattr(p, 'country_of_origin', '') or "",
                    "Warranty": row.get("Warranty") or p.warranty or "",
                    "Weight": row.get("Weight") or (str(p.weight) if p.weight else ""),
                    "Weight_Unit": row.get("Weight_Unit") or p.weight_unit or "",
                    "Length": row.get("Length") or (str(getattr(p, 'length', '')) if getattr(p, 'length', None) else ""),
                    "Width": row.get("Width") or (str(getattr(p, 'width', '')) if getattr(p, 'width', None) else ""),
                    "Height": row.get("Height") or (str(getattr(p, 'height', '')) if getattr(p, 'height', None) else ""),
                    "Currency": row.get("Currency") or p.currency or "",
                    "Base Price": row.get("Base Price") or (str(p.base_price) if p.base_price else ""),
                    "Vendor_Name": row.get("Vendor_Name") or p.vendor_name or "",
                    "Short_Description": row.get("Short_Description") or p.short_description or "",
                    "Long_Description": row.get("Long_Description") or p.long_description or "",
                    "Meta_Title": row.get("Meta_Title") or p.meta_title or "",
                    "Meta_Description": row.get("Meta_Description") or getattr(p, 'meta_description', '') or "",
                    "Search_Keywords": row.get("Search_Keywords") or getattr(p, 'search_keywords', '') or "",
                })
                features_data = ai_data.pop(
                    "features", None) or p.features or []
                if isinstance(features_data, str):
                    try:
                        import json
                        features_data = json.loads(features_data)
                    except:
                        features_data = [features_data]
                if isinstance(features_data, list):
                    for i, feat in enumerate(features_data[:10], 1):
                        row[f"features_{i}"] = clean_for_excel(feat)
                if not row.get("image_url_1"):
                    if hasattr(p, 'images') and p.images:
                        images_list = list(p.images.values()) if isinstance(
                            p.images, dict) else (p.images if isinstance(p.images, list) else [])
                        for i, img in enumerate(images_list[:8], 1):
                            if isinstance(img, dict):
                                row[f"image_name_{i}"] = img.get("name", "")
                                row[f"image_url_{i}"] = img.get("url", "")
                            else:
                                row[f"image_url_{i}"] = str(img) if img else ""
                # Build original attributes map - handle duplicates by using list instead of dict
                original_attrs_by_norm = {}  # normalized_name -> [list of attrs]
                if p.dynamic_attributes:
                    for attr in p.dynamic_attributes:
                        if isinstance(attr, dict) and attr.get('name'):
                            k_norm = normalize_attr_name(attr['name'])
                            if k_norm not in original_attrs_by_norm:
                                original_attrs_by_norm[k_norm] = []
                            original_attrs_by_norm[k_norm].append(attr)

                used_ai_keys = set()
                used_original_indexes = {}  # track which original attr was used {norm_key: index}

                for i, template_attr_name in enumerate(attribute_template, 1):
                    if i > MAX_ATTRIBUTES:
                        break

                    row[f"attribute_name{i}"] = template_attr_name
                    template_norm = normalize_attr_name(template_attr_name)

                    ai_match_key = None
                    for ai_key in ai_data.keys():
                        if ai_key in used_ai_keys or ai_key.lower() in IGNORED_KEYS:
                            continue
                        ai_norm = normalize_attr_name(ai_key)
                        if template_norm == ai_norm or (template_norm in ai_norm and len(template_norm) > 3):
                            ai_match_key = ai_key
                            break

                    ai_val_str = ""
                    ai_uom_str = ""

                    if ai_match_key:
                        raw_val = ai_data[ai_match_key]
                        ai_val_str = clean_for_excel(
                            raw_val, template_attr_name)
                        if isinstance(raw_val, dict):
                            uom_obj = raw_val.get("uom") or raw_val.get("unit")
                            ai_uom_str = clean_for_excel(uom_obj)
                        used_ai_keys.add(ai_match_key)

                    # Get next available original attribute (handle duplicates)
                    orig_val_str = ""
                    orig_uom_str = ""
                    if template_norm in original_attrs_by_norm:
                        attrs_list = original_attrs_by_norm[template_norm]
                        next_idx = used_original_indexes.get(template_norm, 0)
                        if next_idx < len(attrs_list):
                            orig_match = attrs_list[next_idx]
                            orig_val_str = clean_for_excel(orig_match.get('value'))
                            orig_uom_str = clean_for_excel(
                                orig_match.get('uom') or orig_match.get('unit'))
                            used_original_indexes[template_norm] = next_idx + 1

                    final_val = ai_val_str if ai_val_str else orig_val_str
                    final_uom = ai_uom_str if ai_val_str else orig_uom_str

                    row[f"attribute_value{i}"] = final_val
                    row[f"attribute_uom{i}"] = final_uom
                    if is_validation_mode:
                        if ai_val_str and orig_val_str:
                            if ai_val_str.lower().strip() != orig_val_str.lower().strip():
                                row[f"validation_value{i}"] = orig_val_str
                                row[f"validation_uom{i}"] = orig_uom_str
                remaining_attrs = [k for k in ai_data.keys(
                ) if k not in used_ai_keys and k.lower() not in IGNORED_KEYS]
                current_slot = len(attribute_template) + 1
                for ai_key in remaining_attrs:
                    if current_slot > MAX_ATTRIBUTES:
                        break
                    row[f"attribute_name{current_slot}"] = ai_key.replace(
                        '_', ' ').title()
                    row[f"attribute_value{current_slot}"] = clean_for_excel(
                        ai_data[ai_key], ai_key)
                    if isinstance(ai_data[ai_key], dict):
                        uom = ai_data[ai_key].get("uom", "")
                        if uom and uom.lower() not in ["n/a", "na", "none", "null"]:
                            row[f"attribute_uom{current_slot}"] = clean_for_excel(
                                uom)
                    current_slot += 1
                if p.sources_consulted and isinstance(p.sources_consulted, list):
                    for i, url in enumerate(p.sources_consulted[:5], 1):
                        row[f"source_url_{i}"] = url
                sanitized_row = {str(k): sanitize_for_excel(v)
                                 for k, v in row.items()}
                export_rows.append(sanitized_row)
            df = pd.DataFrame(export_rows, columns=all_headers)
            excel_buffer = io.BytesIO()
            with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                df.to_excel(writer, index=False, sheet_name='Enriched Data')
            excel_buffer.seek(0)
            filename = f"Enriched_Export_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
            return StreamingResponse(
                excel_buffer,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f"attachment; filename={filename}"}
            )
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
        for r in rows:
            if str(r.get('sku', '')).strip() or str(r.get('mpn', '')).strip() or str(r.get('product_name', '')).strip():
                valid_rows.append(r)

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
            
            project=await db_session.get(Project,source.project_id)
            if not project or not project.use_case:
                logger.error(f"Project or use_case not found for source {source_id}")
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
                    existing_data={}
                    if product.dynamic_attributes:
                        for attr in product.dynamic_attributes:
                            if isinstance(attr,dict) and attr.get('name'):
                                existing_data[attr['name']]={
                                    'value':attr.get('value'),
                                    'uom':attr.get('uom') or attr.get('unit')
                                }
                    logger.info(f"🔍 EXISTING DATA BUILT for {product.product_code}:")
                    logger.info(f"   dynamic_attributes count: {len(product.dynamic_attributes) if product.dynamic_attributes else 0}")
                    logger.info(f"   existing_data keys: {list(existing_data.keys())}")
                    logger.info(f"   existing_data sample: {dict(list(existing_data.items())[:2])}")
                    for k, v in existing_data.items():
                        logger.info(f"  {k}: value='{v.get('value')}', uom='{v.get('uom')}'")
                    if existing_data:
                        logger.info(f" Excel attributes: {list(existing_data.keys())[:5]}")
                        
                    logger.info(f"Primary attributes found in DB: {primary_attr_names}")
                    aggregation_result = await aggregate_product(
                        mpn=product.product_code,
                        title=product.product_name,
                        brand=product.brand_name,
                        taxonomy=product.taxonomy,
                        primary_attributes=primary_attr_names,
                        existing_data=existing_data, 
                        db=db_session,
                        project_id=source.project_id
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
                        
                        # Apply merge logic with order preservation for backfilling/validation
                        use_case = project.use_case.lower() if project and project.use_case else ""
                        if "back filling" in use_case or "validation" in use_case:
                            merged_attrs = merge_attributes_preserving_order(
                                primary_attributes=primary_attr_names,
                                existing_attrs=existing_data,
                                ai_data=sanitize_ai_data(ai_attributes)
                            )
                            product.attributes = merged_attrs
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
                "aggregation_status": "completed",
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
