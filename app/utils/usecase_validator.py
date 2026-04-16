from typing import List, Dict, Any, Optional
import logging
logger = logging.getLogger('validate_against_usecase')


def get_row_identifier(row: Dict, idx: int) -> str:
    return (
        row.get('SKU') or row.get('sku') or
        row.get('MPN') or row.get('mpn') or
        f"Row {idx}"
    )


def has_category(row: Dict) -> bool:
    return bool(
        row.get("category 1") or
        row.get("category_1") or
        row.get("Taxonomy") or
        row.get("taxonomy")
    )


def get_category_value(row: Dict) -> Optional[str]:
    return (
        row.get("category 1") or
        row.get("category_1") or
        row.get("Taxonomy") or
        row.get("taxonomy")
    )


def has_basic_info(row: Dict) -> bool:
    has_sku = bool(row.get("SKU") or row.get("sku")
                   or row.get("MPN") or row.get("mpn"))
    has_name = bool(row.get("Product_Name") or row.get("product_name"))
    return has_sku and has_name


def count_attributes(row: Dict) -> int:
    count = 0
    for i in range(1, 21):
        attr_name = row.get(f"attribute_name{i}")
        attr_value = row.get(f"attribute_value{i}")
        if attr_name and attr_value:
            count += 1
    dyn_attrs = row.get("dynamic_attributes", [])
    if isinstance(dyn_attrs,list):
        for attr in dyn_attrs:
            if isinstance(attr, dict):
                name = attr.get("name", "")
                value = attr.get("value", "")
                if name and value:
                    count+=1
    return count


def has_any_attribute(row: Dict) -> bool:
    return count_attributes(row) > 0


def format_error_list(items: List[str], max_show: int = 10) -> str:
    if not items:
        return ""
    result = "\n".join(items[:max_show])
    if len(items) > max_show:
        result += f"\n...and {len(items) - max_show} more"
    return result


def find_missing_categories(rows: List[Dict]) -> List[str]:
    missing = []
    for idx, row in enumerate(rows, start=2):
        if not has_category(row):
            sku = get_row_identifier(row, idx)
            missing.append(f"Row {idx} ({sku})")
    return missing


def find_missing_attributes(rows: List[Dict], min_required: int = 1) -> List[str]:
    missing = []
    for idx, row in enumerate(rows, start=2):
        attr_count = count_attributes(row)
        if attr_count < min_required:
            sku = get_row_identifier(row, idx)
            missing.append(f"Row {idx} ({sku}): {attr_count} attribute(s)")
    return missing


def validate_file_against_use_case(rows: List[Dict[str, Any]], use_case: str) -> Dict[str, Any]:
    if not rows:
        return {
            'valid': False,
            'error': 'File is empty or could not be parsed',
            'requirements': []
        }
    use_case_lower = use_case.lower() if use_case else ""
    if 'data cleaning and standardization' in use_case_lower:
        return validate_cleaning_requirements(rows, use_case)
    elif 'products with category assignments' in use_case_lower and 'back filling' not in use_case_lower:
        return validate_with_categories_flexible(rows, use_case)
    elif 'products without category assignments' in use_case_lower:
        return validate_without_categories(rows, use_case)
    elif 'back filling' in use_case_lower and 'validation' not in use_case_lower:
        return validate_backfill_requirements(rows, use_case)
    elif 'validation' in use_case_lower:
        return validate_validation_requirements(rows, use_case)
    else:
        logger.warning(f"Unknown use case: {use_case}. Accepting file.")
        return {
            "valid": False,
            "message": "No specific validation applied",
            "requirements": []
        }


def validate_with_categories_flexible(rows: List[Dict], use_case: str) -> Dict[str, Any]:
    missing_categories = find_missing_categories(rows)
    if missing_categories:
        return {
            'valid': False,
            "error": (
                f"Missing categories in {len(missing_categories)} product(s):\n"
                f"{format_error_list(missing_categories)}\n\n"
                "All products MUST have a category (category 1 or Taxonomy column)."
            ),
            "requirements": [
                "✓ Categories required for ALL products",
                "✓ Attributes are optional (can be mixed in same file)",
                "  → Products WITH attributes will use them as extraction hints",
                "  → Products WITHOUT attributes will discover based on category"
            ]
        }
    products_with_attrs = sum(1 for row in rows if has_any_attribute(row))
    products_without_attrs = len(rows) - products_with_attrs
    logger.info(
        f"Validation passed: {products_with_attrs} with attributes, "
        f"{products_without_attrs} without attributes"
    )
    return {
        "valid": True,
        "message": (
            f"File validated successfully.\n\n"
            f"Statistics:\n"
            f"  • {products_with_attrs} products WITH attributes (will use as hints)\n"
            f"  • {products_without_attrs} products WITHOUT attributes (will discover from category)"
        ),
        "requirements": [
            f"Categories present in all {len(rows)} products",
            f"{products_with_attrs} products have attributes",
            f"{products_without_attrs} products will discover attributes"
        ],
        "statistics": {
            "total_products": len(rows),
            "with_attributes": products_with_attrs,
            "without_attributes": products_without_attrs
        }
    }


def validate_backfill_requirements(rows: List[Dict], use_case: str) -> Dict[str, Any]:
    missing_categories = find_missing_categories(rows)
    missing_attributes = find_missing_attributes(rows, min_required=1)
    errors = []
    if missing_categories:
        errors.append(
            f"Missing categories in {len(missing_categories)} product(s):\n"
            f"{format_error_list(missing_categories)}"
        )
    if missing_attributes:
        errors.append(
            f"Missing attributes in {len(missing_attributes)} product(s):\n"
            f"{format_error_list(missing_attributes)}"
        )
    if errors:
        return {
            'valid': False,
            "error": "\n\n".join(errors),
            'requirements': [
                "✓ Categories required (category 1 or Taxonomy)",
                "✓ At least 1 attribute per product required",
                "✓ System will keep Excel values and backfill missing attributes"
            ]
        }
    return {
        'valid': True,
        "message": "File ready for backfilling. Excel values will be kept, missing attributes will be added.",
        "requirements": ["Categories and attributes present"]
    }
    
def validate_cleaning_requirements(rows: List[Dict], use_case: str) -> Dict[str, Any]:
    
    missing_identifiers = []
    missing_attributes = []
    products_with_attrs = []
    
    for idx, row in enumerate(rows, start=2):
        sku = get_row_identifier(row, idx)
        
        has_identifier = bool(
            row.get('SKU') or row.get('sku') or
            row.get('MPN') or row.get('mpn') or
            row.get('product_code') or row.get('Product Code')
        )
        
        if not has_identifier:
            missing_identifiers.append(f"Row {idx} ({sku or 'unknown'})")
            continue
        
        attr_count = count_attributes(row)
        if attr_count == 0:
            missing_attributes.append(f"Row {idx} ({sku}): 0 attributes")
        else:
            products_with_attrs.append(f"Row {idx} ({sku}): {attr_count} attribute(s)")
    
    errors = []
    
    if missing_identifiers:
        errors.append(
            f"Missing product identifiers (MPN/SKU) in {len(missing_identifiers)} product(s):\n"
            f"{format_error_list(missing_identifiers)}\n\n"
            "Each product MUST have either MPN or SKU to identify it for cleaning."
        )
    
    if missing_attributes:
        errors.append(
            f"Missing attributes in {len(missing_attributes)} product(s):\n"
            f"{format_error_list(missing_attributes)}\n\n"
            "Data cleaning requires at least ONE attribute per product to clean.\n"
            "Please provide attribute_name and attribute_value columns."
        )
    
    if errors:
        return {
            'valid': False,
            'error': "\n\n".join(errors),
            'requirements': [
                "✓ MPN or SKU required for each product",
                "✓ At least 1 attribute per product required",
                "✓ Attribute format: attribute_name1, attribute_value1, attribute_uom1",
                "✓ System will clean and standardize the provided attributes"
            ]
        }
    
    total_products = len(rows)
    total_attrs = sum(count_attributes(row) for row in rows)
    avg_attrs = total_attrs / total_products if total_products > 0 else 0
    
    logger.info(
        f"Cleaning validation passed: {total_products} products, "
        f"{total_attrs} total attributes, avg {avg_attrs:.1f} per product"
    )
    
    return {
        'valid': True,
        'message': (
            f"File validated for cleaning.\n\n"
            f"Statistics:\n"
            f"  • {total_products} products ready for cleaning\n"
            f"  • {total_attrs} total attributes to clean\n"
            f"  • Average {avg_attrs:.1f} attributes per product\n\n"
            f"System will clean and standardize all provided attributes."
        ),
        'requirements': [
            f"✓ {total_products} products with identifiers",
            f"✓ {total_attrs} attributes to clean",
            f"✓ At least 1 attribute per product present"
        ],
        'statistics': {
            'total_products': total_products,
            'total_attributes': total_attrs,
            'avg_attributes_per_product': round(avg_attrs, 1),
            'products_with_attributes': len(products_with_attrs)
        }
    }

def validate_validation_requirements(rows: List[Dict], use_case: str) -> Dict[str, Any]:
    missing_categories = find_missing_categories(rows)
    insufficient_attributes = find_missing_attributes(rows, min_required=2)
    errors = []
    if missing_categories:
        errors.append(
            f"Missing categories in {len(missing_categories)} product(s):\n"
            f"{format_error_list(missing_categories)}"
        )
    if insufficient_attributes:
        errors.append(
            f"Insufficient attributes in {len(insufficient_attributes)} product(s):\n"
            f"{format_error_list(insufficient_attributes)}\n\n"
            "Validation mode requires at least 2 attributes per product.\n"
            "System will verify Excel data against web sources and flag conflicts."
        )
    if errors:
        return {
            "valid": False,
            "error": "\n\n".join(errors),
            "requirements": [
                "✓ Categories required",
                "✓ At least 2 attributes per product required",
                "✓ System will verify Excel data against web sources",
                "✓ Conflicts will be flagged in validation columns"
            ]
        }
    return {
        "valid": True,
        "message": "File ready for validation. Excel data will be verified against web sources.",
        "requirements": ["Categories and multiple attributes present"]
    }


def validate_without_categories(rows: List[Dict], use_case: str) -> Dict[str, Any]:
    missing_basic_info = []
    products_with_categories = []
    for idx, row in enumerate(rows, start=2):
        sku = get_row_identifier(row, idx)
        if not has_basic_info(row):
            missing_basic_info.append(f"Row {idx}")
        if has_category(row):
            category = get_category_value(row)
            products_with_categories.append(f"Row {idx} ({sku}): {category}")
    errors = []
    if missing_basic_info:
        errors.append(
            f"Missing basic info (SKU/MPN or Product Name) in {len(missing_basic_info)} product(s):\n"
            f"{format_error_list(missing_basic_info)}"
        )
    if products_with_categories:
        errors.append(
            f"{len(products_with_categories)} product(s) have categories filled:\n"
            f"{format_error_list(products_with_categories)}\n\n"
            "For 'Without categories' use case, category columns should be EMPTY.\n"
            "System will auto-classify products into categories."
        )
    if errors:
        return {
            "valid": False,
            "error": "\n\n".join(errors),
            "requirements": [
                "✓ SKU/MPN required",
                "✓ Product Name required",
                "✓ Categories should be EMPTY (system will auto-classify)",
                "✓ Attributes optional"
            ]
        }
    return {
        "valid": True,
        "message": "File validated. Ready for auto-classification.",
        "requirements": ["Basic product info present", "No categories (will auto-classify)"]
    }
