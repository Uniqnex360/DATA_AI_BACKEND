
from sqlite3 import IntegrityError
from typing import Optional
from uuid import UUID
import logging

from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.brand import Brand
from app.models.vendor import Vendor
from app.models.industry import Industry
from app.models.category import Category
logger = logging.getLogger("batch_import")
from sqlalchemy.exc import IntegrityError

async def get_or_create_brand(db: AsyncSession, name: Optional[str]) -> Optional[Brand]:
    if not name:
        return None
    
    name_clean = name.strip()
    normalized = name_clean.lower().replace(" ", "")
    
    stmt = select(Brand).where(Brand.normalized_name == normalized)
    result = await db.execute(stmt)
    brand = result.scalars().first()
    if brand:
        return brand
    
    stmt = select(Brand).where(Brand.name == name_clean)
    result = await db.execute(stmt)
    brand = result.scalars().first()
    if brand:
        return brand
    
    try:
        brand = Brand(name=name_clean, normalized_name=normalized)
        db.add(brand)
        await db.flush()
        await db.refresh(brand)
        logger.info(f"Created new brand: {brand.name}")
        return brand
    except IntegrityError:
        await db.rollback()
        stmt = select(Brand).where(
            (Brand.normalized_name == normalized) | (Brand.name == name_clean)
        )
        result = await db.execute(stmt)
        brand = result.scalars().first()
        if brand:
            logger.info(f"Brand '{name_clean}' created by concurrent request, using existing")
            return brand
        raise
        
        
        
        
    


async def get_or_create_vendor(db: AsyncSession, name: Optional[str]) -> Optional[Vendor]:
    if not name:
        return None
    normalized = name.lower().replace(" ", "")
    stmt = select(Vendor).where(Vendor.normalized_name == normalized)
    result = await db.execute(stmt)
    vendor = result.scalars().first()
    if vendor:
        return vendor

    vendor = Vendor(name=name.strip(), normalized_name=normalized)
    db.add(vendor)
    await db.commit()
    await db.refresh(vendor)
    logger.info(f"Created new vendor: {vendor.name}")
    return vendor


async def get_or_create_industry(db: AsyncSession, name: Optional[str]) -> Optional[Industry]:
    if not name:
        return None
    stmt = select(Industry).where(Industry.name == name.strip())
    result = await db.execute(stmt)
    industry = result.scalars().first()
    if industry:
        return industry

    industry = Industry(name=name.strip())
    db.add(industry)
    await db.commit()
    await db.refresh(industry)
    logger.info(f"Created new industry: {industry.name}")
    return industry