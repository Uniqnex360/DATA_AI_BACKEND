
import io
import logging
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import  HTTPException, status


logger = logging.getLogger("batch_import")

def parse_import_file(file_bytes: bytes, filename: str) -> List[Dict[str, Any]]:
    
    try:
        if filename.lower().endswith(".csv"):
            df = pd.read_csv(io.BytesIO(file_bytes))
        else:
            df = pd.read_excel(io.BytesIO(file_bytes))
    except Exception as e:
        logger.error(f"Failed to read import file {filename}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unable to read file: {e}",
        )

    if df.empty:
        raise HTTPException(status_code=400, detail="Import file is empty")

    df.columns = [str(c).strip() for c in df.columns]

    rows: List[Dict[str, Any]] = []

    for _, row in df.iterrows():
        r = {k: ("" if pd.isna(v) else str(v).strip())for k, v in row.to_dict().items()}
        dynamic_attrs = []
        for i in range(1, 41):
            attr_name = r.get(f'attribute_name{i}')
            if attr_name and str(attr_name).strip():
                dynamic_attrs.append({
                    'name': str(attr_name).strip(),
                    'value': r.get(f'attribute_value{i}', ''),
                    'uom': r.get(f'attribute_uom{i}', ''),
                    'validation_value': r.get(f'validation_value{i}', ''),
                    'validation_uom': r.get(f'validation_uom{i}', '')
                })
        
        product = {
            "prod_id": r.get("Prod ID") or r.get("prod_id"),
            "sku": r.get("SKU") or r.get("sku"),
            "product_type": r.get("Product_Type") or r.get("product_type"),
            "parent_sku": r.get("Parent_SKU") or r.get("parent_sku"),
            "product_name": r.get("Product_Name") or r.get("product_name"),
            "brand": r.get("Brand") or r.get("brand"),
            "vendor": r.get("Vendor") or r.get("vendor"),
            "gtin": r.get("GTIN") or r.get("gtin"),
            "ean": r.get("ean"),
            "upc": r.get("upc"),
            "unspc": r.get("unspc"),
            "mpn": r.get("MPN") or r.get("mpn"),
            "status": r.get("Status") or r.get("status"),
            "lifecycle_stage": r.get("Lifecycle_Stage") or r.get("lifecycle_stage"),
            "launch_date": r.get("Launch_Date") or r.get("launch_date"),
            "discontinue_status": r.get("Discontinue_Status") or r.get("discontinue_status"),

            "industry_name": r.get("industry_name"),
            "category_1": r.get("category 1") or r.get("category_1"),
            "category_2": r.get("category 2") or r.get("category_2"),
            "category_3": r.get("category 3") or r.get("category_3"),
            "category_4": r.get("category 4") or r.get("category_4"),
            "category_5": r.get("category 5") or r.get("category_5"),
            "category_6": r.get("category 6") or r.get("category_6"),
            "category_7": r.get("category 7") or r.get("category_7"),
            "category_8": r.get("category 8") or r.get("category_8"),
            "taxonomy": (r.get("Taxonomy") or r.get("taxonomy") or "").strip() or None,

            "country_of_origin": r.get("Country_of_Origin") or r.get("country_of_origin"),
            "warranty": r.get("Warranty") or r.get("warranty"),
            "weight": r.get("Weight"),
            "weight_unit": r.get("Weight_Unit") or r.get("weight_unit"),
            "length": r.get("Length"),
            "width": r.get("Width") or r.get("Widt") or r.get("width"),
            "height": r.get("Height") or r.get("height"),
            'attributes': dynamic_attrs,
            'primary_attributes': dynamic_attrs[:5] if dynamic_attrs else []
        }

        rows.append(product)

    return rows


def infer_taxonomy_for_row(
    row: Dict[str, Any],
    all_rows: List[Dict[str, Any]],
) -> Optional[str]:
    
    if row.get("taxonomy"):
        return row["taxonomy"]

    mpn = (row.get("mpn") or "").strip()
    name = (row.get("product_name") or "").strip().lower()

    if mpn:
        for other in all_rows:
            if other is row:
                continue
            if (other.get("mpn") or "").strip() == mpn and other.get("taxonomy"):
                return other["taxonomy"]

    if name:
        for other in all_rows:
            if other is row:
                continue
            tax = other.get("taxonomy")
            if not tax:
                continue
            other_name = (other.get("product_name") or "").strip().lower()
            if not other_name:
                continue
            similarity = SequenceMatcher(None, name, other_name).ratio()
            if similarity >= 0.8:
                return tax

    return None

