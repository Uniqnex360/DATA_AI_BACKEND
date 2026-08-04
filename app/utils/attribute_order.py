from typing import List, Tuple, Optional, Set
from uuid import UUID
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import Product
from app.models.project_product_link import ProjectProductLink
from app.models.attribute import Attribute
from app.models.product_attribute_link import ProductAttributeLinkModel
from app.aggregation.prompt_builder import get_taxonomy_attribute_hints

# NOTE: Ideally import these from a shared constants module that your export uses.
# For now, keep a minimal “dedicated keys” set in sync with export mapping keys.
DEDICATED_KEYS = {
    "name","product_name","title","brand","manufacturer","manufacturer part number",
    "manufacturer_part_number","model_number","model number","sku","mpn","brand_name",
    "model","category","taxonomy","product_code","product_type","type","parent_sku",
    "gtin","ean","upc","unspc","status","lifecycle_stage","launch_date",
    "discontinue_status","weight","weight_unit","length","width","height",
    "dimension_unit","dimensions","country_of_origin","made_in","warranty",
    "warranty_period","price","base_price","list_price","sale_price",
    "selling_price","special_price","currency","stock","stock_qty","quantity",
    "stock_status","availability","vendor","vendor_name","supplier","vendor_sku",
    "description","short_description","product_description","long_description",
    "detailed_description","product_summary","meta_title","meta_description",
    "keywords","search_keywords","seo_keywords","certification","certifications",
    "safety_standard","safety_standards","hazardous","hazardous_material",
    "prop65","prop65_warning","image","image_url","main_image","3d_model","model_3d",
}

# Keep this aligned with your export IGNORED_KEYS (you can move the full set here later).
IGNORED_KEYS = {
    "workflow_stage","enrichment_status","categories","category","category_1","category_2",
    "category_3","category_4","category_5","category_6","category_7","category_8",
}

def normalize_attr_name(s: str) -> str:
    return (s or "").strip().lower().replace("_", "").replace(" ", "").replace("-", "")

def _allowed(name: str, ignored_norm: Set[str], dedicated_norm: Set[str]) -> bool:
    n = normalize_attr_name(name)
    return bool(n) and n not in ignored_norm and n not in dedicated_norm

async def build_attribute_order_for_project_taxonomy(
    db: AsyncSession,
    project_id: UUID,
    taxonomy: str,
    max_attributes: int = 100,
) -> Tuple[List[str], List[str]]:
    """
    Returns:
      - attribute_order: list[str] in the same order export uses
      - category_attribute_names: list[str] (so UI can hide them if it wants)
    """
    taxonomy = taxonomy or "Unknown"

    # 1) Category attrs (export includes these)
    category_attrs = []
    if taxonomy != "Unknown":
        category_attrs = await get_taxonomy_attribute_hints(taxonomy, db)

    # 2) User-defined attrs from normalized tables across project+taxonomy
    user_stmt = (
        select(Attribute.attribute_name)
        .join(ProductAttributeLinkModel, ProductAttributeLinkModel.attribute_id == Attribute.id)
        .join(Product, Product.id == ProductAttributeLinkModel.product_id)
        .join(ProjectProductLink, ProjectProductLink.product_id == Product.id)
        .where(
            ProjectProductLink.project_id == project_id,
            Product.taxonomy == taxonomy
        )
        .distinct()
    )
    user_rows = (await db.execute(user_stmt)).all()
    user_defined = [r[0] for r in user_rows if r and r[0]]

    # 3) AI discovered attrs from Product.attributes JSON across project+taxonomy
    prod_stmt = (
        select(Product.attributes)
        .join(ProjectProductLink, ProjectProductLink.product_id == Product.id)
        .where(
            ProjectProductLink.project_id == project_id,
            Product.taxonomy == taxonomy
        )
    )
    prod_rows = (await db.execute(prod_stmt)).scalars().all()
    ai_discovered = set()
    for attrs in prod_rows:
        if isinstance(attrs, dict):
            ai_discovered.update(attrs.keys())

    ignored_norm = {normalize_attr_name(k) for k in IGNORED_KEYS}
    dedicated_norm = {normalize_attr_name(k) for k in DEDICATED_KEYS}

    template: List[str] = []
    added = set()

    def add_if_unique(name: str):
        if not _allowed(name, ignored_norm, dedicated_norm):
            return
        n = normalize_attr_name(name)
        if n in added:
            return
        added.add(n)
        template.append(name)

    for n in user_defined:
        add_if_unique(n)

    for n in category_attrs:
        add_if_unique(n)

    for n in sorted(ai_discovered):
        add_if_unique(n)

    return template[:max_attributes], category_attrs