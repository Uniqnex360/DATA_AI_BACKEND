from typing import Any, List, Optional, Dict
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_session
from app.models.attribute import Attribute
from app.models.brand import Brand
from app.services.product_service import product_service
from sqlmodel import select, func,and_
from app.schemas.product import ProductCreate, ProductResponse
import logging
from uuid import UUID
import uuid
from app.models.project import Project
from app.models.product import Product
from app.models.product_attribute_link import ProductAttributeLinkModel
logger = logging.getLogger('products')
router = APIRouter()


@router.get("/", response_model=Dict[str, Any])
async def read_products(
    db: AsyncSession = Depends(get_session),
    project_id: Optional[UUID] = None,
    enrichment_status: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    workflow_stage: Optional[str] = None,
    brand_name:Optional[str]=None,
    category_1:Optional[str]=None,
    search:Optional[str]=None
):
    try:
        statement = select(Product)
        if project_id:
            statement = statement.where(Product.project_id == project_id)
        if workflow_stage and hasattr(Product, 'workflow_stage'):
            statement = statement.where(
                Product.workflow_stage == workflow_stage)
        if enrichment_status and enrichment_status != 'all':
            statement = statement.where(Product.enrichment_status== enrichment_status)
        if brand_name:
            statement=statement.where(Product.brand_name==brand_name)
        if category_1:
            statement=statement.where(Product.category_1==category_1)
        if search:
            search_term=f"%{search}%"
            statement=statement.where((Product.product_name.ilike(search_term))|
                                      (Product.product_code.ilike(search_term))|
                                      (Product.brand_name.ilike(search_term)))
        count_stmt = select(func.count()).select_from(statement.subquery())
        count_result = await db.execute(count_stmt)
        total = count_result.scalar() or 0
        statement = statement.order_by(Product.created_at.desc()).offset(skip).limit(limit)
        result = await db.execute(statement)
        products = result.scalars().all()
        project_data = None
        if project_id:
            project_data = await db.get(Project, project_id)
        product_list = []
        for p in products:
            p_dict = p.dict() if hasattr(p, 'dict') else p.__dict__
            existing = set()
            if p.attributes:
                existing.update(p.attributes.keys())
            attr_count = 0
            try:
                attr_stmt = (
                    select(Attribute.attribute_name)
                    .join(ProductAttributeLinkModel, ProductAttributeLinkModel.attribute_id == Attribute.id)
                    .where(ProductAttributeLinkModel.product_id == p.id)
                )
                attr_result = await db.execute(attr_stmt)
                for row in attr_result.all():
                    existing.add(row[0])
                    attr_count+=1
            except Exception:
                pass
            p_dict['attribute_count'] = attr_count
            expected = await get_expected_attributes_async(p, db)
            p_dict['missing_attributes'] = [
                a for a in expected if a not in existing]
            product_list.append(p_dict)
        return {
            "products": product_list,
            "total": total,
            "project": project_data,
            "skip": skip,
            "limit": limit
        }
    except Exception as e:
        logger.error(f"API Error: {str(e)}")
        return {"products": [], "total": 0, "project": None}


@router.post('/', response_model=ProductResponse)
async def create_product(*, db: AsyncSession = Depends(get_session), product_in: ProductCreate):
    return await product_service.create(db=db, obj_in=product_in)


@router.get("/filters")
async def get_products_filters(
    project_id: str | None = None,
    brand_name: Optional[str] = None,
    category_1: Optional[str] = None,
    workflow_stage: str | None = None,
    db: AsyncSession = Depends(get_session),
):
    try:
        category_stmt = select(Product.category_1).where(Product.category_1.isnot(None))
        brand_stmt = select(Brand.name).join(Product, Product.brand_id == Brand.id).where(Brand.name.isnot(None))
        
        if project_id:
            category_stmt = category_stmt.where(Product.project_id == project_id)
            brand_stmt = brand_stmt.where(Product.project_id == project_id)
        if workflow_stage:
            category_stmt = category_stmt.where(Product.workflow_stage == workflow_stage)
            brand_stmt = brand_stmt.where(Product.workflow_stage == workflow_stage)
        if brand_name:
            category_stmt = category_stmt.where(Product.brand_name == brand_name)
        if category_1:
            brand_stmt = brand_stmt.where(Product.category_1 == category_1)
        
        category_result = await db.execute(category_stmt.distinct())
        category_rows = category_result.all()
        categories = sorted([row[0].strip() for row in category_rows if row[0]])
        
        brand_result = await db.execute(brand_stmt.distinct())
        brand_rows = brand_result.all()
        brands = sorted([row[0].strip() for row in brand_rows if row[0]])
        
        return {"categories": categories, "brands": brands}
    except Exception as e:
        logger.error(f"Failed to fetch filters for project {project_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to fetch project filters")


@router.get("/attributes")
async def get_project_attributes(
        project_id: str = Query(..., description="Project ID"),
        category: str | None = Query(
            default=None, description="Optional category filter"),
        db: AsyncSession = Depends(get_session),):
    try:
        # stmt = select(Product.dynamic_attributes).where(
        #     Product.project_id == project_id)
        # if category:
        #     stmt = stmt.where(Product.category_1 == category)
        # result = await db.execute(stmt)
        # rows = result.scalars().all()
        # attribute_names = set()
        # for dynamic_attributes in rows:
        #     if not dynamic_attributes or not isinstance(dynamic_attributes, list):
        #         continue
        #     for attr in dynamic_attributes:
        #         if isinstance(attr, dict):
        #             name = attr.get('name')
        #             if isinstance(name, str) and name.strip():
        # 
        # attribute_names.add(name.strip())
        stmt = (
            select(Attribute.attribute_name)
            .join(ProductAttributeLinkModel, ProductAttributeLinkModel.attribute_id == Attribute.id)
            .join(Product, Product.id == ProductAttributeLinkModel.product_id)
            .where(Product.project_id == project_id)
            .distinct()
        )
        if category:
            stmt = stmt.where(Product.category_1 == category)
        result = await db.execute(stmt)
        attribute_names = set(row[0] for row in result.all())
        return {
            "attributes": sorted(attribute_names)
        }
    except Exception as e:
        logger.error(
            f"Failed to fetch attributes for project {project_id} and category {category}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch project attributes",
        )


async def get_expected_attributes_async(product: Product, db: AsyncSession) -> List[str]:
    try:
        if not product.taxonomy and not product.category_1:
            return []
        stmt = select(Product).where(
            Product.id != product.id,
            Product.enrichment_status == "completed",
        )
        if product.taxonomy:
            stmt = stmt.where(Product.taxonomy == product.taxonomy)
        elif product.category_1:
            stmt = stmt.where(Product.category_1 == product.category_1)
        stmt = stmt.limit(10)
        result = await db.execute(stmt)
        similar_products = result.scalars().all()
        if not similar_products:
            return []
        attribute_counts = {}
        for p in similar_products:
            # attrs = set()
            # if p.attributes:
            #     attrs.update(p.attributes.keys())
            # if p.dynamic_attributes:
            #     for attr in p.dynamic_attributes:
            #         if isinstance(attr, dict) and attr.get("name"):
            #             attrs.add(attr["name"])
            attrs = set()
            if p.attributes:
                attrs.update(p.attributes.keys())
            try:
                attr_stmt = (
                    select(Attribute.attribute_name)
                    .join(ProductAttributeLinkModel, ProductAttributeLinkModel.attribute_id == Attribute.id)
                    .where(ProductAttributeLinkModel.product_id == p.id)
                )
                attr_result = await db.execute(attr_stmt)
                for row in attr_result.all():
                    attrs.add(row[0])
            except Exception:
                pass
            for attr in attrs:
                attribute_counts[attr] = attribute_counts.get(attr, 0) + 1
        threshold = max(1, len(similar_products) * 0.3)
        expected = [
            attr for attr, count in attribute_counts.items() if count >= threshold
        ]
        return sorted(expected)
    except Exception:
        return []


@router.get("/stats/project/{project_id}")
async def get_project_product_stats(
    project_id: UUID,
    brand_name: Optional[str] = None,
    category_1: Optional[str] = None,
    search: Optional[str] = None,
    enrichment_status: Optional[str] = None,
    bulk_attributes: Optional[List[str]] = Query(None),
    db: AsyncSession = Depends(get_session)
) -> Dict[str, int]:
    try:
        stmt = select(Product).where(Product.project_id == project_id)
        if brand_name:
            stmt = stmt.where(Product.brand_name == brand_name)
        if category_1:
            stmt = stmt.where(Product.category_1 == category_1)
        if enrichment_status and enrichment_status != 'all':
            stmt = stmt.where(Product.enrichment_status == enrichment_status)
        if search:
            search_term = f"%{search}%"
            stmt = stmt.where(
                (Product.product_name.ilike(search_term)) |
                (Product.product_code.ilike(search_term)) |
                (Product.brand_name.ilike(search_term))
            )
        result = await db.execute(stmt)
        products = result.scalars().all()
        if bulk_attributes:
            filtered_products = []
            for product in products:
                product_attrs = set()
                try:
                    attr_stmt = (
                        select(Attribute.attribute_name)
                        .join(ProductAttributeLinkModel, ProductAttributeLinkModel.attribute_id == Attribute.id)
                        .where(ProductAttributeLinkModel.product_id == product.id)
                    )
                    attr_result = await db.execute(attr_stmt)
                    for row in attr_result.all():
                        product_attrs.add(row[0])
                except Exception:
                    pass
                if all(attr in product_attrs for attr in bulk_attributes):
                    filtered_products.append(product)
            products = filtered_products
        stats = {
            "total": len(products),
            "completed": sum(1 for p in products if p.enrichment_status == "completed"),
            "pending": sum(1 for p in products if p.enrichment_status == "pending"),
            "processing": sum(1 for p in products if p.enrichment_status == "processing"),
            "failed": sum(1 for p in products if p.enrichment_status == "failed"),
        }
        return stats
    except Exception as e:
        logger.error(f"Failed to fetch stats for project {project_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to fetch project statistics"
        )


@router.get("/stats/projects/batch")
async def get_multiple_project_stats(
    project_ids: List[UUID] = Query(...),
    db: AsyncSession = Depends(get_session)
) -> Dict[str, Dict[str, int]]:
    try:
        result = {}
        for project_id in project_ids:
            stmt = select(
                Product.enrichment_status,
                func.count(Product.id).label('count')
            ).where(
                Product.project_id == project_id
            ).group_by(Product.enrichment_status)
            query_result = await db.execute(stmt)
            rows = query_result.all()
            stats = {
                "total": 0,
                "completed": 0,
                "pending": 0,
                "processing": 0,
                "failed": 0,
            }
            for status, count in rows:
                stats["total"] += count
                if status == "completed":
                    stats["completed"] = count
                elif status == "pending":
                    stats["pending"] = count
                elif status == "processing":
                    stats["processing"] = count
                elif status == "failed":
                    stats["failed"] = count
            result[str(project_id)] = stats
        return result
    except Exception as e:
        logger.error(f"Failed to fetch batch stats: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to fetch project statistics"
        )
@router.get("/enrichment-counts", response_model=Dict[str, int])
async def get_enrichment_counts(
    db: AsyncSession = Depends(get_session)
):
    
    try:
        stmt = select(
            Product.project_id,
            func.count(Product.id).label('count')
        ).where(
            and_(
                Product.workflow_stage == 'enrichment',
                Product.enrichment_status == 'pending'
            )
        ).group_by(Product.project_id)
        
        result = await db.execute(stmt)
        counts = {str(row[0]): row[1] for row in result.all()}
        return counts
    except Exception as e:
        logger.error(f"Error getting enrichment counts: {str(e)}")
        return {}