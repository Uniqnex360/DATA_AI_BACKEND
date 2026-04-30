"""
One-time migration script to move existing product.dynamic_attributes data
into normalized tables (attribute_master, attribute_value, 
product_attribute_link, product_attribute_value_link).
"""
import asyncio
import logging
from typing import Optional
from uuid import UUID

# IMPORTANT: Load all models before importing Product to resolve relationships
import app.models.industry
import app.models.brand
import app.models.category
import app.models.attribute
import app.models.vendor
import app.models.project
import app.models.product
import app.models.product_attribute_link

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.database import async_session_factory
from app.models.product import Product
from app.models.attribute import Attribute, AttributeValue, CategoryAttribute
from app.models.category import Category
from app.models.product_attribute_link import ProductAttributeLinkModel, ProductAttributeValueLinkModel

logger = logging.getLogger("migrate_dynamic_attrs")
logging.basicConfig(level=logging.INFO)


async def ensure_category_from_path(db: AsyncSession, path_parts: list) -> Optional[UUID]:
    """Ensure category hierarchy exists, return leaf category ID"""
    try:
        if not path_parts:
            return None
        
        parent_id = None
        leaf_id = None
        
        for idx, cat_name in enumerate(path_parts):
            cat_name = str(cat_name).strip()
            if not cat_name:
                continue
            
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
        
        return leaf_id
    except Exception as e:
        logger.error(f"Error creating category path {path_parts}: {e}")
        return None


async def migrate_product_attributes(
    db: AsyncSession,
    product: Product,
    dry_run: bool = False
) -> dict:
    """Migrate a single product's dynamic_attributes to normalized tables"""
    result = {
        "product_code": product.product_code,
        "attributes_found": 0,
        "attributes_migrated": 0,
        "category_linked": False,
        "errors": []
    }
    
    if not product.dynamic_attributes:
        return result
    
    result["attributes_found"] = len(product.dynamic_attributes)
    
    # Ensure category exists
    path_parts = []
    for i in range(1, 9):
        cat = getattr(product, f'category_{i}', None)
        if cat and str(cat).strip():
            path_parts.append(str(cat).strip())
    
    category_id = None
    if path_parts:
        category_id = await ensure_category_from_path(db, path_parts)
        if category_id and not dry_run:
            product.category_id = category_id
            db.add(product)
            result["category_linked"] = True
    
    # Migrate each attribute
    for attr in product.dynamic_attributes:
        try:
            if not isinstance(attr, dict):
                continue
            
            attr_name = str(attr.get('name', '')).strip()
            if not attr_name:
                continue
            
            attr_value = attr.get('value')
            attr_uom = attr.get('uom') or attr.get('unit')
            validation_value = attr.get('validation_value')
            validation_uom = attr.get('validation_uom')
            
            if dry_run:
                result["attributes_migrated"] += 1
                continue
            
            # 1. Get or create Attribute
            attr_stmt = select(Attribute).where(Attribute.attribute_name == attr_name)
            attr_result = await db.execute(attr_stmt)
            attribute = attr_result.scalars().first()
            
            if not attribute:
                import uuid as _uuid
                base_code = attr_name.lower().replace(" ", "_").replace("/", "_").replace("-", "_")
                # Check if code already exists
                code_check = await db.execute(
                    select(Attribute).where(Attribute.attribute_code == base_code)
                )
                if code_check.scalars().first():
                    base_code = f"{base_code}_{_uuid.uuid4().hex[:6]}"
                
                attribute = Attribute(
                    attribute_name=attr_name,
                    attribute_code=base_code,
                    data_type="string",
                )
                db.add(attribute)
                await db.flush()
            
            # 2. Link Product → Attribute
            link_stmt = select(ProductAttributeLinkModel).where(
                ProductAttributeLinkModel.product_id == product.id,
                ProductAttributeLinkModel.attribute_id == attribute.id,
            )
            if not (await db.execute(link_stmt)).scalars().first():
                db.add(ProductAttributeLinkModel(
                    product_id=product.id,
                    attribute_id=attribute.id,
                ))
            
            # 3. Link Category → Attribute
            if category_id:
                ca_stmt = select(CategoryAttribute).where(
                    CategoryAttribute.category_id == category_id,
                    CategoryAttribute.attribute_id == attribute.id,
                )
                if not (await db.execute(ca_stmt)).scalars().first():
                    db.add(CategoryAttribute(
                        category_id=category_id,
                        attribute_id=attribute.id,
                    ))
            
            # 4. Create AttributeValue + link
            if attr_value is not None and str(attr_value).strip():
                val_stmt = select(AttributeValue).where(
                    AttributeValue.attribute_id == attribute.id,
                    AttributeValue.value == str(attr_value).strip(),
                )
                val_result = await db.execute(val_stmt)
                attr_val = val_result.scalars().first()
                
                if not attr_val:
                    attr_val = AttributeValue(
                        attribute_id=attribute.id,
                        value=str(attr_value).strip(),
                        uom=str(attr_uom) if attr_uom else None,
                        validation_value=str(validation_value) if validation_value else None,
                        validation_uom=str(validation_uom) if validation_uom else None,
                    )
                    db.add(attr_val)
                    await db.flush()
                
                pv_stmt = select(ProductAttributeValueLinkModel).where(
                    ProductAttributeValueLinkModel.product_id == product.id,
                    ProductAttributeValueLinkModel.attribute_value_id == attr_val.id,
                )
                if not (await db.execute(pv_stmt)).scalars().first():
                    db.add(ProductAttributeValueLinkModel(
                        product_id=product.id,
                        attribute_value_id=attr_val.id,
                    ))
            
            result["attributes_migrated"] += 1
            
        except Exception as e:
            error_msg = f"Error migrating attribute '{attr.get('name', 'unknown')}': {e}"
            logger.error(error_msg)
            result["errors"].append(error_msg)
    
    return result


async def run_migration(dry_run: bool = True, batch_size: int = 100):
    """Run the full migration"""
    async with async_session_factory() as db:
        logger.info("=" * 60)
        logger.info(f"STARTING DYNAMIC ATTRIBUTES MIGRATION {'(DRY RUN)' if dry_run else '(LIVE)'}")
        logger.info("=" * 60)
        
        
        # Use a different approach to count
        all_products_stmt = select(Product)
        all_result = await db.execute(all_products_stmt)
        all_products = all_result.scalars().all()
        
        products_with_attrs = [
            p for p in all_products 
            if p.dynamic_attributes and len(p.dynamic_attributes) > 0
        ]
        
        total = len(products_with_attrs)
        logger.info(f"Found {total} products with dynamic_attributes")
        
        if total == 0:
            logger.info("No products to migrate!")
            return
        
        # Process in batches
        total_migrated = 0
        total_errors = 0
        total_categories = 0
        
        for i in range(0, total, batch_size):
            batch = products_with_attrs[i:i + batch_size]
            
            for product in batch:
                try:
                    result = await migrate_product_attributes(db, product, dry_run)
                    total_migrated += result["attributes_migrated"]
                    total_errors += len(result["errors"])
                    if result["category_linked"]:
                        total_categories += 1
                    
                    if result["attributes_found"] > 0:
                        logger.info(
                            f"  {result['product_code']}: "
                            f"{result['attributes_migrated']}/{result['attributes_found']} attrs migrated"
                        )
                    
                    if not dry_run:
                        await db.commit()
                except Exception as e:
                    logger.error(f"Failed to migrate {product.product_code}: {e}")
                    await db.rollback()
                    total_errors += 1
                    continue
            

        
        logger.info("=" * 60)
        logger.info("MIGRATION SUMMARY:")
        logger.info(f"  Products processed: {total}")
        logger.info(f"  Attributes migrated: {total_migrated}")
        logger.info(f"  Categories linked: {total_categories}")
        logger.info(f"  Errors: {total_errors}")
        logger.info("=" * 60)
        
        if dry_run:
            logger.info("\n✅ DRY RUN COMPLETE - No changes were made.")
            logger.info("Run with dry_run=False to execute the migration.")
        else:
            logger.info("\n✅ MIGRATION COMPLETE")


if __name__ == "__main__":
    import sys
    
    dry_run = "--live" not in sys.argv
    
    if dry_run:
        print("\n🔍 Running in DRY RUN mode (no changes will be made)")
        print("   Use --live flag to execute the migration\n")
    else:
        print("\n⚠️  LIVE MIGRATION - Changes will be saved to database!")
        response = input("   Type 'yes' to continue: ")
        if response.lower() != 'yes':
            print("   Aborted.")
            sys.exit(0)
    
    asyncio.run(run_migration(dry_run=dry_run))