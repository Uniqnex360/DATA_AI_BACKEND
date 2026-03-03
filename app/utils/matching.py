
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

async def get_or_create_brand(db: AsyncSession, name: Optional[str]) -> Optional[Brand]:
    if not name:
        return None
    normalized = name.lower().replace(" ", "")
    stmt = select(Brand).where(Brand.normalized_name == normalized)
    result = await db.execute(stmt)
    brand = result.scalars().first()
    if brand:
        return brand

    brand = Brand(name=name.strip(), normalized_name=normalized)
    db.add(brand)
    await db.commit()
    await db.refresh(brand)
    logger.info(f"Created new brand: {brand.name}")
    return brand


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