import re
import io
from fastapi import HTTPException
import pandas as pd
from datetime import datetime
from typing import List, Optional
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.aggregation.prompt_builder import get_taxonomy_attribute_hints
from app.models.product import Product
from app.models.project import Project
import json

async def generate_products_excel(
    products: List[Product],
    db: AsyncSession,
    global_project_name: Optional[str] = None,
    filename: Optional[str] = None 
) -> StreamingResponse:
    if not products:
        raise HTTPException(status_code=404, detail="No products to export")
    use_case = None
    first_project = await db.get(Project, products[0].project_id)
    if first_project and first_project.use_case:
        use_case = first_project.use_case.lower()
    if 'back filling' in use_case or 'validation' in use_case:
        MAX_ATTRIBUTES = 100
    else:
        MAX_ATTRIBUTES = 100
    core_headers = ["Sequence","Prod ID", "SKU", "Product_Type", "Parent_SKU", "Product_Name", "Brand", "GTIN",
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
        media_headers.extend([f"document_name_{i}", f"document_url_{i}"])
    content_headers = ["3D_Model_URL", "Short_Description", "Long_Description",
                       "features_1", "features_2", "features_3", "features_4", "features_5",
                       "features_6", "features_7", "features_8", "features_9", "features_10",
                       "Meta_Title", "Meta_Description", "Search_Keywords",
                       "Certification", "Safety_Standard", "Hazardous_Material", "Prop65_Warning"]
    attr_headers = []
    for i in range(1, MAX_ATTRIBUTES + 1):
        attr_headers.extend([
            f"attribute_name{i}", f"attribute_value{i}",
            f"attribute_uom{i}", f"validation_value{i}", f"validation_uom{i}"
        ])
    source_url_headers = [f"source_url_{i}" for i in range(1, 6)]
    all_headers = ["Project Name"] + core_headers + cat_headers + phys_headers + price_headers + \
                  media_headers + content_headers + attr_headers + source_url_headers
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
        "price_range", "usage", "voc_level", 'category'
    }
    def normalize_attr_name(s: str) -> str:
        return s.strip().lower().replace('_', '').replace(' ', '').replace('-', '')
    taxonomy_raw_data = {}
    for p in products:
        tax = p.taxonomy or "Unknown"
        if tax not in taxonomy_raw_data:
            taxonomy_raw_data[tax] = {
                'user_defined': [],
                'user_defined_map': {},
                'ai_discovered': set(),
            }
        data = taxonomy_raw_data[tax]
        try:
            from app.models.attribute import Attribute
            from app.models.product_attribute_link import ProductAttributeLinkModel, ProductAttributeValueLinkModel
            attr_stmt = (
                select(Attribute.attribute_name)
                .join(ProductAttributeLinkModel, ProductAttributeLinkModel.attribute_id == Attribute.id)
                .where(ProductAttributeLinkModel.product_id == p.id)
            )
            attr_result = await db.execute(attr_stmt)
            for row in attr_result.all():
                name = row[0].strip()
                name_norm = normalize_attr_name(name)
                if name and name_norm not in data['user_defined_map']:
                    data['user_defined'].append(name)
                    data['user_defined_map'][name_norm] = name
        except Exception:
            pass
        if p.attributes:
            for key in p.attributes.keys():
                key_lower = key.lower().strip()
                if key_lower in IGNORED_KEYS:
                    continue
                key_norm = key_lower.replace('_', '').replace(' ', '').replace('-', '')
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
                for map_key in DEDICATED_COLUMN_MAPPING.keys():
                    if norm == normalize_attr_name(map_key):
                        return False
                added_normalized.add(norm)
                final_template.append(name)
                return True
            return False
        for attr in data['user_defined']:
            add_if_unique(attr)
        if tax != "Unknown":
            category_attrs = await get_taxonomy_attribute_hints(tax, db)
            for cat_attr in category_attrs:
                add_if_unique(cat_attr)
        for ai_attr in sorted(data['ai_discovered']):
            add_if_unique(ai_attr)
        taxonomy_templates[tax] = final_template[:MAX_ATTRIBUTES]
    is_validation_mode = 'validation' in use_case if use_case else False
    project_name_cache = {}
    if not global_project_name:
        project_ids = {p.project_id for p in products}
        if project_ids:
            stmt = select(Project.id, Project.name).where(Project.id.in_(list(project_ids)))
            result = await db.execute(stmt)
            project_name_cache = {row[0]: row[1] for row in result.fetchall()}
    export_rows = []
    for p in products:
        row = {h: "" for h in all_headers}
        if global_project_name:
            row["Project Name"] = global_project_name
        else:
            row["Project Name"] = project_name_cache.get(p.project_id, "")
        ai_data = dict(p.attributes or {})
        taxonomy = p.taxonomy or "Unknown"
        attribute_template = taxonomy_templates.get(taxonomy, [])
        from app.api.v1.endpoints.extraction import clean_for_excel, sanitize_for_excel
        
        if 'images' in ai_data and isinstance(ai_data['images'], list):
        
            images_list = ai_data.pop('images')
            for i, img_url in enumerate(images_list[:8], 1):
                row[f'image_url_{i}'] = clean_for_excel(img_url)
        elif 'image_url' in ai_data:
            row['image_url_1'] = clean_for_excel(ai_data.pop('image_url'))
        elif 'main_image' in ai_data:
            row['image_url_1'] = clean_for_excel(ai_data.pop('main_image'))
        elif 'image' in ai_data:
            row['image_url_1'] = clean_for_excel(ai_data.pop('image'))
        if not row.get("image_url_1") and p.image_url_1:
            row["image_url_1"] = p.image_url_1
        for ai_key in list(ai_data.keys()):
            ai_key_norm = ai_key.lower().replace('_', '').replace(' ', '').replace('-', '')
            for map_key, target_col in DEDICATED_COLUMN_MAPPING.items():
                map_key_norm = map_key.lower().replace('_', '').replace(' ', '').replace('-', '')
                if ai_key_norm == map_key_norm:
                    value = ai_data.pop(ai_key)
                    row[target_col] = clean_for_excel(value)
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
             "Sequence": p.aggregation_index or "", 
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
        features_data = ai_data.pop("features", None) or p.features or []
        if isinstance(features_data, str):
            try:
                features_data = json.loads(features_data)
            except:
                features_data = [features_data]
        if isinstance(features_data, list):
            for i, feat in enumerate(features_data[:10], 1):
                row[f"features_{i}"] = clean_for_excel(feat)
        if not row.get("image_url_1") and hasattr(p, 'images') and p.images:
            images_list = list(p.images.values()) if isinstance(p.images, dict) else (p.images if isinstance(p.images, list) else [])
            for i, img in enumerate(images_list[:8], 1):
                if isinstance(img, dict):
                    row[f"image_name_{i}"] = img.get("name", "")
                    row[f"image_url_{i}"] = img.get("url", "")
                else:
                    row[f"image_url_{i}"] = str(img) if img else ""
        # original_attrs_by_norm = {}
        # if p.dynamic_attributes:
        #     for attr in p.dynamic_attributes:
        #         if isinstance(attr, dict) and attr.get('name'):
        #             k_norm = normalize_attr_name(attr['name'])
        #             if k_norm not in original_attrs_by_norm:
        #                 original_attrs_by_norm[k_norm] = []
        #             original_attrs_by_norm[k_norm].append(attr)
        original_attrs_by_norm = {}
        try:
            from app.models.attribute import Attribute, AttributeValue
            val_stmt = (
                select(Attribute.attribute_name, AttributeValue.value, AttributeValue.uom,
                       AttributeValue.validation_value, AttributeValue.validation_uom)
                .join(AttributeValue, AttributeValue.attribute_id == Attribute.id)
                .join(ProductAttributeValueLinkModel, 
                      ProductAttributeValueLinkModel.attribute_value_id == AttributeValue.id)
                .where(ProductAttributeValueLinkModel.product_id == p.id)
            )
            val_result = await db.execute(val_stmt)
            for attr_name, value, uom, validation_value, validation_uom in val_result.all():
                k_norm = normalize_attr_name(attr_name)
                if k_norm not in original_attrs_by_norm:
                    original_attrs_by_norm[k_norm] = []
                original_attrs_by_norm[k_norm].append({
                    'name': attr_name,
                    'value': value,
                    'uom': uom,
                    'validation_value': validation_value,
                    'validation_uom': validation_uom
                })
        except Exception:
            pass
        used_ai_keys = set()
        used_original_indexes = {}
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
                ai_val_str = clean_for_excel(raw_val, template_attr_name)
                if isinstance(raw_val, dict):
                    uom_obj = raw_val.get("uom") or raw_val.get("unit")
                    ai_uom_str = clean_for_excel(uom_obj)
                used_ai_keys.add(ai_match_key)
            orig_val_str = ""
            orig_uom_str = ""
            orig_match = None
            if template_norm in original_attrs_by_norm:
                attrs_list = original_attrs_by_norm[template_norm]
                next_idx = used_original_indexes.get(template_norm, 0)
                if next_idx < len(attrs_list):
                    orig_match = attrs_list[next_idx]
                    orig_val_str = clean_for_excel(orig_match.get('value'))
                    orig_uom_str = clean_for_excel(orig_match.get('uom') or orig_match.get('unit'))
                    used_original_indexes[template_norm] = next_idx + 1
            final_val = ai_val_str if ai_val_str else orig_val_str
            final_uom = ai_uom_str if ai_val_str else orig_uom_str
            row[f"attribute_value{i}"] = final_val
            row[f"attribute_uom{i}"] = final_uom
            if orig_match and orig_match.get('validation_value'):
                validation_val = clean_for_excel(orig_match.get('validation_value'))
                original_val = clean_for_excel(orig_match.get('value'))
                def normalize_for_display(val):
                    if val is None:
                        return ""
                    val_str = str(val).lower().strip()
                    val_str = re.sub(r'\s*(lbs?|kg|g|in|cm|mm|ft|°[fc]|degrees?)\s*\.?\s*$', '', val_str)
                    val_str = re.sub(r'[^\d\.\-]', '', val_str)
                    return val_str
                norm_original = normalize_for_display(original_val)
                norm_validation = normalize_for_display(validation_val)
                if validation_val and norm_validation != norm_original:
                    row[f"validation_value{i}"] = validation_val
                    row[f"validation_uom{i}"] = clean_for_excel(orig_match.get('validation_uom'))
            elif p.validation_conflicts:
                if template_attr_name in p.validation_conflicts:
                    row[f"validation_value{i}"] = clean_for_excel(p.validation_conflicts[template_attr_name])
                else:
                    temp_norm = normalize_attr_name(template_attr_name)
                    for conflict_key, conflict_val in p.validation_conflicts.items():
                        if normalize_attr_name(conflict_key) == temp_norm:
                            row[f"validation_value{i}"] = clean_for_excel(conflict_val)
                            break
            if not row.get(f"validation_value{i}") and is_validation_mode:
                if ai_val_str and orig_val_str:
                    if ai_val_str.lower().strip() != orig_val_str.lower().strip():
                        row[f"validation_value{i}"] = orig_val_str
                        row[f"validation_uom{i}"] = orig_uom_str
        remaining_attrs = []
        for k in ai_data.keys():
            if k in used_ai_keys or k.lower() in IGNORED_KEYS:
                continue
            # NEW CHECK: Skip if matches dedicated column
            k_norm = normalize_attr_name(k)
            is_dedicated = False
            for map_key in DEDICATED_COLUMN_MAPPING.keys():
                if k_norm == normalize_attr_name(map_key):
                    is_dedicated = True
                    break
            if not is_dedicated:
                remaining_attrs.append(k)
        
        # NOW process remaining_attrs (separate loop)
        current_slot = len(attribute_template) + 1
        for ai_key in remaining_attrs:
            if current_slot > MAX_ATTRIBUTES:
                break
            row[f"attribute_name{current_slot}"] = ai_key.replace('_', ' ').title()
            row[f"attribute_value{current_slot}"] = clean_for_excel(ai_data[ai_key], ai_key)
            if isinstance(ai_data[ai_key], dict):
                uom = ai_data[ai_key].get("uom", "")
                if uom and uom.lower() not in ["n/a", "na", "none", "null"]:
                    row[f"attribute_uom{current_slot}"] = clean_for_excel(uom)
            current_slot += 1
        if p.source_url:
            row["source_url_1"] = p.source_url
        if p.sources_consulted and isinstance(p.sources_consulted, list):
            brand = row.get('Brand') or p.brand_name or ""
            urls = list(p.sources_consulted)
            def normalize_for_brand(s:str)->str:
                return s.lower().replace('-', '').replace('_', '')
            norm_brand=normalize_for_brand(brand)
            urls.sort(key=lambda u: 0 if (norm_brand and norm_brand in normalize_for_brand(u)) else 1)
            for i, url in enumerate(urls[:5], 1):
                row[f"source_url_{i}"] = url
        export_rows.append({str(k): sanitize_for_excel(v) for k, v in row.items()})
    df = pd.DataFrame(export_rows, columns=all_headers)
    excel_buffer = io.BytesIO()
    with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Enriched Data')
    excel_buffer.seek(0)
    if not filename:
        filename ="Enriched_Export.xlsx"
    if not filename.endswith('.xlsx'):
        filename=f"{filename}.xlsx"
        
    from urllib.parse import quote
    encoded_filename = quote(filename)
    return StreamingResponse(
        excel_buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"}
    )