import logging
from typing import Optional, List
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from uuid import uuid4
from app.models.product import Product
from app.models.attribute import Attribute, AttributeValue
from app.models.category import Category
from app.models.attribute import CategoryAttribute
import re
from app.models.product_attribute_link import ProductAttributeLinkModel, ProductAttributeValueLinkModel
logger = logging.getLogger("attribute_normalizer")
async def get_category_expected_attributes(db: AsyncSession, category_id: UUID) -> List[str]:
    if not category_id:
        return []
    try:
        stmt = (
            select(Attribute.attribute_name)
            .join(CategoryAttribute, CategoryAttribute.attribute_id == Attribute.id)
            .where(CategoryAttribute.category_id == category_id)
            .order_by(CategoryAttribute.display_order)
        )
        result = await db.execute(stmt)
        return [row[0] for row in result.all()]
    except Exception as e:
        logger.warning(f"Failed to get category attributes for {category_id}: {e}")
        return []
async def ensure_category_from_path(db: AsyncSession, path_parts: list):
    try:
        if not path_parts:
            return None
        parent_id = None
        leaf_id = None
        for idx, cat_name in enumerate(path_parts):
            try:
                if not cat_name or not str(cat_name).strip():
                    continue
                cat_name = str(cat_name).strip()
                level = idx + 1
                current_path = " > ".join(path_parts[:level])
                stmt = select(Category).where(Category.full_path == current_path)
                result = await db.execute(stmt)
                existing = result.scalars().first()
                if existing:
                    parent_id = existing.id
                    leaf_id = existing.id
                    continue
                is_leaf = (idx == len(path_parts) - 1)
                category = Category(
                    name=cat_name,
                    full_path=current_path,
                    level=level,
                    parent_category_id=parent_id,
                    industry_id=None,  
                    is_leaf=is_leaf,
                    is_active=True,
                )
                db.add(category)
                await db.flush()
                parent_id = category.id
                leaf_id = category.id
            except Exception as e:
                logger.error(f"Error creating category '{cat_name}' at level {level}: {e}")
                continue
        return leaf_id
    except Exception as e:
        logger.error(f"Failed to ensure category from path {path_parts}: {e}")
        return None
async def save_attributes_normalized(
    db: AsyncSession,
    product: Product,
    dynamic_attrs: List[dict],
    category_id: Optional[UUID] = None
):
    if not dynamic_attrs:
        return
    saved_count = 0
    error_count = 0
    for attr in dynamic_attrs:
        try:
            if not isinstance(attr, dict) or not attr.get('name'):
                continue
            attr_name = str(attr['name']).strip()
            if not attr_name:
                continue
            attr_value = attr.get('value')
            attr_uom = attr.get('uom') or attr.get('unit')
            try:
                attribute = await get_or_create_attribute(db, attr_name)
            except Exception as e:
                logger.error(f"Failed to get/create attribute '{attr_name}': {e}")
                error_count += 1
                continue
            try:
                await ensure_product_attribute_link(db, product.id, attribute.id)
            except Exception as e:
                logger.error(f"Failed to link product {product.id} to attribute {attribute.id}: {e}")
            if category_id:
                try:
                    await ensure_category_attribute_link(db, category_id, attribute.id)
                except Exception as e:
                    logger.error(f"Failed to link category {category_id} to attribute {attribute.id}: {e}")
            if attr_value is not None and str(attr_value).strip():
                try:
                    attr_val = await get_or_create_attribute_value(
                        db, attribute.id, str(attr_value).strip(), str(attr_uom) if attr_uom else None
                    )
                    await ensure_product_attribute_value_link(db, product.id, attr_val.id)
                except Exception as e:
                    logger.error(f"Failed to save attribute value '{attr_value}' for '{attr_name}': {e}")
            saved_count += 1
        except Exception as e:
            logger.error(f"Unexpected error processing attribute {attr.get('name', 'unknown')}: {e}", exc_info=True)
            error_count += 1
            continue
    logger.info(f"Attributes normalized for product {product.id}: {saved_count} saved, {error_count} errors")
def normalize_attr_name(name: str) -> str:
    """
    Normalize attribute name for case-insensitive comparison.
    Matches the function used in the database index.
    """
    if not name:
        return ""
    return re.sub(
        re.compile(r'\s+'),
        ' ',
        re.sub(r'[^a-z0-9]', '', name.lower())
    ).strip()
async def get_or_create_attribute(db: AsyncSession, name: str) -> Attribute:
    """
    Get or create an attribute. Uses normalize_attr_name to check for
    case-insensitive duplicates (matches database index).
    """
    try:
        normalized_name = normalize_attr_name(name)
        if not normalized_name:
            raise ValueError(f"Invalid attribute name: {name}")
        all_attrs_stmt = select(Attribute)
        result = await db.execute(all_attrs_stmt)
        all_attrs = result.scalars().all()
        existing_attr = None
        for attr in all_attrs:
            if normalize_attr_name(attr.attribute_name) == normalized_name:
                existing_attr = attr
                break
        if existing_attr:
            logger.info(f"Attribute '{name}' found (existing: '{existing_attr.attribute_name}')")
            return existing_attr
        attribute_code = (
            name.lower()
            .replace(" ", "_")
            .replace("/", "_")
            .replace("-", "_")
            .replace("(", "")
            .replace(")", "")
            .replace(".", "")
            .strip("_")
        )
        existing_code = await db.execute(
            select(Attribute).where(Attribute.attribute_code == attribute_code)
        )
        if existing_code.scalars().first():
            attribute_code = f"{attribute_code}_{uuid4().hex[:6]}"
        attribute = Attribute(
            attribute_name=name,
            attribute_code=attribute_code,
            data_type="string",
        )
        db.add(attribute)
        try:
            await db.flush()
            await db.refresh(attribute)
            logger.info(f"Created new attribute: {name} (id: {attribute.id})")
            return attribute
        except Exception as flush_error:
            await db.rollback()
            logger.warning(f"Race condition for '{name}', trying lookup again: {flush_error}")
            all_attrs_stmt = select(Attribute)
            result = await db.execute(all_attrs_stmt)
            all_attrs = result.scalars().all()
            for attr in all_attrs:
                if normalize_attr_name(attr.attribute_name) == normalized_name:
                    return attr
            raise flush_error
    except Exception as e:
        logger.error(f"Error in get_or_create_attribute for '{name}': {e}", exc_info=True)
        raise
async def get_or_create_attribute_value(
    db: AsyncSession,
    attribute_id: UUID,
    value: str,
    uom: Optional[str] = None
) -> AttributeValue:
    try:
        stmt = select(AttributeValue).where(
            AttributeValue.attribute_id == attribute_id,
            AttributeValue.value == value,
        )
        result = await db.execute(stmt)
        existing = result.scalars().first()
        if existing:
            if uom and not existing.uom:
                existing.uom = uom
                db.add(existing)
                await db.flush()
            return existing
        attr_value = AttributeValue(
            attribute_id=attribute_id,
            value=value,
            uom=uom if uom else None,
        )
        db.add(attr_value)
        await db.flush()
        await db.refresh(attr_value)
        logger.info(f"Created attribute value: {value} for attribute {attribute_id}")
        return attr_value
    except Exception as e:
        logger.error(f"Error in get_or_create_attribute_value: {e}", exc_info=True)
        raise
async def ensure_product_attribute_link(
    db: AsyncSession,
    product_id: UUID,
    attribute_id: UUID
):
    try:
        stmt = select(ProductAttributeLinkModel).where(
            ProductAttributeLinkModel.product_id == product_id,
            ProductAttributeLinkModel.attribute_id == attribute_id,
        )
        result = await db.execute(stmt)
        existing = result.scalars().first()
        if existing:
            return  
        link = ProductAttributeLinkModel(
            product_id=product_id,
            attribute_id=attribute_id,
        )
        db.add(link)
        try:
            await db.flush()  
            logger.info(f"Linked product {product_id} to attribute {attribute_id}")
        except Exception as e:
            await db.rollback()
            logger.info(f"Link already exists for product {product_id} and attribute {attribute_id}")
    except Exception as e:
        logger.error(f"Error linking product {product_id} to attribute {attribute_id}: {e}")
async def ensure_category_attribute_link(
    db: AsyncSession,
    category_id: UUID,
    attribute_id: UUID
):
    try:
        stmt = select(CategoryAttribute).where(
            CategoryAttribute.category_id == category_id,
            CategoryAttribute.attribute_id == attribute_id,
        )
        result = await db.execute(stmt)
        existing = result.scalars().first()
        
        if existing:
            return
        
        link = CategoryAttribute(
            category_id=category_id,
            attribute_id=attribute_id,
        )
        db.add(link)
        
        try:
            await db.flush()
            logger.info(f"Linked category {category_id} to attribute {attribute_id}")
        except Exception as e:
            await db.rollback()
            logger.info(f"Link already exists for category {category_id} and attribute {attribute_id}")
            
    except Exception as e:
        logger.error(f"Error linking category {category_id} to attribute {attribute_id}: {e}")
async def ensure_product_attribute_value_link(
    db: AsyncSession,
    product_id: UUID,
    attribute_value_id: UUID
):
    try:
        stmt = select(ProductAttributeValueLinkModel).where(
            ProductAttributeValueLinkModel.product_id == product_id,
            ProductAttributeValueLinkModel.attribute_value_id == attribute_value_id,
        )
        result = await db.execute(stmt)
        existing = result.scalars().first()
        
        if existing:
            return
        
        link = ProductAttributeValueLinkModel(
            product_id=product_id,
            attribute_value_id=attribute_value_id,
        )
        db.add(link)
        
        try:
            await db.flush()
            logger.info(f"Linked product {product_id} to attribute value {attribute_value_id}")
        except Exception as e:
            await db.rollback()
            logger.info(f"Link already exists for product {product_id} and value {attribute_value_id}")
            
    except Exception as e:
        logger.error(f"Error linking product {product_id} to value {attribute_value_id}: {e}")
