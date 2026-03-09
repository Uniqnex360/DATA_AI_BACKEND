from __future__ import annotations
from sqlmodel import select, and_
from app.models.attribute import Attribute, CategoryAttribute
from app.models.category import Category
from typing import List, Dict, Optional, Any
import logging
from sqlalchemy.ext.asyncio import AsyncSession
logger = logging.getLogger("prompt_builder")


async def build_aggregation_prompt(
    mpn: str,
    product_name: str,
    brand: Optional[str] = None,
    taxonomy: Optional[str] = None,
    primary_attributes: Optional[List[str]] = None,
    existing_data: Optional[Dict[str, Any]] = None,
    db: Optional[AsyncSession] = None,
    use_case: Optional[str] = None,
) -> Dict[str, Any]:
    has_primary_attrs = primary_attributes and len(primary_attributes) > 0
    has_taxonomy = taxonomy and taxonomy.strip()
    context_parts = [f"Product: {product_name}"]
    if mpn:
        context_parts.append(f"MPN: {mpn}")
    if brand:
        context_parts.append(f"Brand: {brand}")
    if taxonomy:
        context_parts.append(f"Category: {taxonomy}")
    context = "\n".join(context_parts)
    naming_rules = """
    
ATTRIBUTE NAMING RULES:
- Use SINGULAR forms: "Material" not "Materials", "Certification" not "Certifications"
- Use Title Case: "Color Temperature" not "color temperature"
- Be consistent: Always use the same name for the same concept
- Common attributes: Material, Dimension, Weight, Color, Certification, Feature, Standard
"""
    strict_extraction_rules = """
STRICT REJECTION RULES (CRITICAL):
1. Do NOT extract website metadata, HTML attributes, file access dates, or server timestamps.
2. Do NOT extract user comments, forum posts, or email headers (e.g., 'subject', 'received').
3. If a requested attribute (like 'Internal Bin Code' or 'Custom ID') is clearly a proprietary internal tracking number and NOT a public specification, you MUST ignore it. Do not invent or guess a value.
4. If the page content is clearly NOT a product page (e.g., it is a forum, a blog about a different topic, or an error page), return an empty attributes object {}.
"""
    has_existing_values=False
    existing_list=[]
    if existing_data and has_primary_attrs:
        for attr in primary_attributes:
            if existing_data.get(attr):
                has_existing_values=True
                break
    if has_primary_attrs and has_existing_values:
        existing_list=[]    
        for attr in primary_attributes:
            val=existing_data.get(attr,'MISSING')
            existing_list.append(f"  • {attr}: {val}")
        existing_text = "\n".join(existing_list)
        prompt=f"""
        You are extracting product specifications from web searches and data sources.
        {context}
        CURRENT DATA (Verify & Enrich):
        {existing_text}

        TASK:
        1. VALIDATE: Check if the "CURRENT DATA" is correct based on official sources. 
        - CRITICAL: If you cannot find information about an attribute (e.g., it is an internal code like a Bin Number), DO NOT change it. Leave it out of your response entirely so we know to keep the original.
        - Only return a value for a 'CURRENT DATA' attribute if you find a definitively DIFFERENT, more accurate value.
        2. DISCOVER: Find technical specifications NOT in the list above.
        {strict_extraction_rules} 

        EXTRACTION RULES:
        1. Extract EXACT values with units.
        2. Return attributes that are NEW or CORRECTED.
        3. Do not simply repeat correct existing data.
        

        OUTPUT FORMAT:
        {{
        "attributes": {{
        "new_attribute": {{"value": "val", "uom": "unit", "confidence": 0.95}},
        "corrected_attribute": {{"value": "new_val", "uom": "unit", "confidence": 0.90}}
        }}
        }}
        Search the web, validate existing data, and discover new specifications."""
        return {
            'prompt':prompt,
            "expected_attributes": primary_attributes + ["*discover*"],
            'mode':'enrichment_validation',
            'primary_count':len(primary_attributes)
        }
    elif has_primary_attrs:
        primary_list = "\n".join(
            [f"  {i+1}. {attr}" for i, attr in enumerate(primary_attributes)])
        prompt = f"""You are extracting product specifications from web searches and data sources.
{context}
EXTRACTION PRIORITY:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PRIMARY ATTRIBUTES (MUST EXTRACT - These are critical):
{primary_list}
ADDITIONAL ATTRIBUTES (Extract any others you discover):
  • Any other technical specifications
  • Physical properties (weight, dimensions)
  • Electrical specifications
  • Performance characteristics
  • Certifications and compliance
  • Material composition
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{strict_extraction_rules}  
EXTRACTION RULES:
1. Extract EXACT values as they appear (preserve units, formatting)
2. If a primary attribute is not found, leave it blank (do NOT guess)
3. Extract up to 20 total attributes (5 primary + 15 additional)
4. Include unit of measurement when applicable
5. Prefer manufacturer data over retailer data
6. Use official product pages when available
STRICT RELEVANCY RULE:
1. Verify the product type: You are looking for a '{taxonomy or product_name}'.
2. If the data you are reading is clearly for a DIFFERENT type of product (e.g., you find 'RAM' or 'Camera' for a 'Light Fixture'), you MUST return an empty "attributes" object.
3. DO NOT mix specifications from different products.
OUTPUT FORMAT:
Return JSON with this structure:
{{
  "attributes": {{
    "{primary_attributes[0] if primary_attributes else 'attribute_name'}": {{"value": "extracted_value", "uom": "unit", "confidence": 0.95}},
    ...
  }}
}}
Search the web and extract these specifications."""
        return {
            "prompt": prompt,
            "expected_attributes": primary_attributes + ["*additional*"],
            "mode": "constrained",
            "priority_count": len(primary_attributes)
        }
    elif has_taxonomy:
        taxonomy_hints = []
        if db:
            taxonomy_hints = await get_taxonomy_attribute_hints(taxonomy, db)
        else:
            logger.warning(
                "No DB session provided, using empty taxonomy hints")
        hints_text = ""
        if taxonomy_hints:
            hints_text = f"""
COMMON ATTRIBUTES FOR THIS CATEGORY:
{chr(10).join([f"  • {attr}" for attr in taxonomy_hints])}
"""
        prompt = f"""You are extracting product specifications from web searches and data sources.
{context}
TASK: Discover and extract ALL relevant technical specifications for this product.
{hints_text}
{naming_rules}
{strict_extraction_rules}
EXTRACTION GUIDELINES:
1. Start with category-typical attributes (listed above)
2. Then extract ANY other specifications you find
3. Aim for 15-20 total attributes
4. Include:
   • Technical specifications
   • Physical properties (weight, dimensions, materials)
   • Electrical/Performance specs
   • Certifications and standards
   • Operating conditions
   • Package contents
EXTRACTION RULES:
- Extract EXACT values (preserve units, formatting)
- Do NOT guess or estimate
- Prefer official manufacturer data
- Include unit of measurement
- Mark confidence level for each attribute
OUTPUT FORMAT:
{{
  "attributes": {{
    "attribute_name": {{"value": "exact_value", "uom": "unit", "confidence": 0.9}},
    ...
  }},
  "discovered_taxonomy": "{taxonomy}"
}}
Search the web and extract comprehensive specifications."""
        return {
            "prompt": prompt,
            "expected_attributes": taxonomy_hints if taxonomy_hints else ["*discover*"],
            "mode": "taxonomy_guided",
            "taxonomy": taxonomy
        }
    else:
        prompt = f"""You are extracting product specifications from web searches and data sources.
{context}
TASK: Perform FULL product discovery and extraction.
PHASE 1 - CLASSIFY:
Determine the product category/taxonomy:
  • What industry? (e.g., Lighting, Tools, Safety, Electronics)
  • What specific category? (e.g., High Bay Lighting, Power Drills, Fall Protection)
  • Full taxonomy path: Industry > Category > Subcategory
PHASE 2 - EXTRACT:
Extract ALL relevant specifications (aim for 15-20 attributes):
{naming_rules} 
{strict_extraction_rules} 
MUST INCLUDE (if applicable):
  • Physical: Dimensions, Weight, Material
  • Technical: Power, Voltage, Capacity, Performance specs
  • Standards: Certifications, Safety ratings, Compliance
  • Operational: Operating conditions, Temperature range
  • Package: What's included, Warranty
CATEGORY-SPECIFIC ATTRIBUTES:
  • Extract attributes typical for this product category
  • Include model/variant details
  • Include compatibility information
EXTRACTION RULES:
- Extract EXACT values (preserve units, formatting)
- Do NOT guess or estimate
- Prefer official manufacturer data
- Include unit of measurement for all numeric values
- Mark confidence level (0.0-1.0) for each attribute
OUTPUT FORMAT:
{{
  "discovered_taxonomy": "Industry > Category > Subcategory",
  "attributes": {{
    "attribute_name": {{"value": "exact_value", "uom": "unit", "confidence": 0.9}},
    ...
  }}
}}
Search the web and perform comprehensive product discovery."""
        return {
            "prompt": prompt,
            "expected_attributes": ["*discover*"],
            "mode": "full_discovery"
        }


async def get_taxonomy_attribute_hints(
    taxonomy: Optional[str],
    db: AsyncSession
) -> List[str]:
    if not taxonomy or not isinstance(taxonomy, str) or not taxonomy.strip():
        return []
    try:
        clean_path = " > ".join([part.strip()
                                for part in taxonomy.split(">") if part.strip()])
        stmt = (
            select(Attribute.display_name)
            .join(CategoryAttribute, Attribute.id == CategoryAttribute.attribute_id)
            .join(Category, CategoryAttribute.category_id == Category.id)
            .where(
                and_(
                    Category.full_path == clean_path,
                    CategoryAttribute.is_primary == True
                )
            )
            .order_by(CategoryAttribute.display_order)
        )
        result = await db.execute(stmt)
        hints = [row[0] for row in result.all()]
        if hints:
            return hints
        path_parts = clean_path.split(" > ")
        while len(path_parts) > 1:
            path_parts.pop()
            parent_path = " > ".join(path_parts)
            parent_stmt = (
                select(Attribute.display_name)
                .join(CategoryAttribute, Attribute.id == CategoryAttribute.attribute_id)
                .join(Category, CategoryAttribute.category_id == Category.id)
                .where(and_(Category.full_path == parent_path, CategoryAttribute.is_primary == True))
                .order_by(CategoryAttribute.display_order)
            )
            parent_result = await db.execute(parent_stmt)
            parent_hints = [row[0] for row in parent_result.all()]
            if parent_hints:
                return parent_hints
        leaf_name = clean_path.split(" > ")[-1]
        leaf_stmt = (
            select(Attribute.display_name)
            .join(CategoryAttribute, Attribute.id == CategoryAttribute.attribute_id)
            .join(Category, CategoryAttribute.category_id == Category.id)
            .where(and_(Category.name == leaf_name, CategoryAttribute.is_primary == True))
            .limit(5)
        )
        result = await db.execute(leaf_stmt)
        return [row[0] for row in result.all()]
    except Exception as e:
        logger.error(f"Error in get_taxonomy_attribute_hints: {str(e)}")
    return []


async def get_taxonomy_attribute_hints_simple(
    taxonomy: str,
    db: AsyncSession
) -> List[str]:
    try:
        stmt = select(Category).where(Category.full_path == taxonomy.strip())
        result = await db.execute(stmt)
        category = result.scalars().first()
        if not category:
            return []
        attr_stmt = (
            select(Attribute.display_name)
            .join(CategoryAttribute, Attribute.id == CategoryAttribute.attribute_id)
            .where(
                CategoryAttribute.category_id == category.id,
                CategoryAttribute.is_primary == True
            )
            .order_by(CategoryAttribute.display_order)
        )
        attr_result = await db.execute(attr_stmt)
        return [row[0] for row in attr_result.all()]
    except Exception as e:
        logger.error(f"Error fetching taxonomy hints: {e}")
        return []
