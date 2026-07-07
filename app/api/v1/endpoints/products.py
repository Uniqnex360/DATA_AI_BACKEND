from typing import Any, List, Optional, Dict
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_session
from app.models.attribute import Attribute, AttributeValue
from app.models.brand import Brand
from app.models.project_product_link import ProjectProductLink
from app.schemas.brand import BrandCreate
from app.services.product_service import product_service
from sqlmodel import select, func, and_, or_ 
from app.schemas.product import ProductCreate, ProductResponse
import logging
from uuid import UUID
import uuid
from app.models.project import Project
from app.models.product import Product
from app.models.product_attribute_link import ProductAttributeLinkModel, ProductAttributeValueLinkModel
from app.models.category import Category
from datetime import datetime
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
    brand_name: Optional[str] = None,
    category_1: Optional[str] = None,
    search: Optional[str] = None
):
    try:
        statement = select(Product, ProjectProductLink.enrichment_status.label('link_status'))
        if project_id:
            statement = statement.join(ProjectProductLink,Product.id==ProjectProductLink.product_id).where(
                ProjectProductLink.project_id == project_id)
        if workflow_stage and hasattr(Product, 'workflow_stage'):
            statement = statement.where(
                Product.workflow_stage == workflow_stage)
        if enrichment_status and enrichment_status != 'all':
            if project_id:
                statement = statement.where(
                    ProjectProductLink.enrichment_status == enrichment_status)
            else:
                statement = statement.where(
                    Product.enrichment_status == enrichment_status)
        if brand_name:
            statement = statement.where(Product.brand_name == brand_name)
        if category_1:
            statement = statement.where(
                or_(
                    Product.category_1 == category_1,
                    Product.category_2 == category_1,
                    Product.category_3 == category_1,
                    Product.category_4 == category_1,
                    Product.category_5 == category_1,
                    Product.category_6 == category_1,
                    Product.category_7 == category_1,
                    Product.category_8 == category_1,
                    Product.taxonomy == category_1,
                )
            )
        if search:
            search_term = f"%{search}%"
            statement = statement.where((Product.product_name.ilike(search_term)) |
                                        (Product.product_code.ilike(search_term)) |
                                        (Product.brand_name.ilike(search_term)))
        # count_stmt = select(func.count()).select_from(statement.subquery())
        if project_id:
            count_stmt = select(func.count(Product.id)).join(
                ProjectProductLink, Product.id == ProjectProductLink.product_id
            ).where(ProjectProductLink.project_id == project_id)
        else:
            count_stmt = select(func.count(Product.id))

        if enrichment_status and enrichment_status != 'all':
            if project_id:
                count_stmt = count_stmt.where(ProjectProductLink.enrichment_status == enrichment_status)
            else:
                count_stmt = count_stmt.where(Product.enrichment_status == enrichment_status)
        if brand_name:
            count_stmt = count_stmt.where(Product.brand_name == brand_name)
        if category_1:
            count_stmt = count_stmt.where(
                or_(
                    Product.category_1 == category_1,
                    Product.category_2 == category_1,
                    Product.category_3 == category_1,
                    Product.category_4 == category_1,
                    Product.category_5 == category_1,
                    Product.category_6 == category_1,
                    Product.category_7 == category_1,
                    Product.category_8 == category_1,
                    Product.taxonomy == category_1,
                )
            )
        
        if search:
            search_term = f"%{search}%"
            count_stmt = count_stmt.where(
                (Product.product_name.ilike(search_term)) |
                (Product.product_code.ilike(search_term)) |
                (Product.brand_name.ilike(search_term))
            )

        count_result = await db.execute(count_stmt)
        total = count_result.scalar() or 0
        statement = statement.order_by(
            Product.created_at.desc()).offset(skip).limit(limit)
        result = await db.execute(statement)
        # products = result.scalars().all()
        rows = result.all()
        project_data = None
        if project_id:
            project_data = await db.get(Project, project_id)
        product_list = []
        for row in rows:
            p = row[0]  # Product object
            link_status = row[1]  # link enrichment_status
            p_dict = p.dict() if hasattr(p, 'dict') else p.__dict__
            p_dict['enrichment_status'] = link_status or p_dict.get('enrichment_status', 'pending')
            # Build attributes_dict from normalized tables
            attributes_dict = {}
            attribute_names = []
            try:
                val_stmt = (
                    select(Attribute.attribute_name,
                           AttributeValue.value, AttributeValue.uom)
                    .join(AttributeValue, AttributeValue.attribute_id == Attribute.id)
                    .join(ProductAttributeValueLinkModel,
                          ProductAttributeValueLinkModel.attribute_value_id == AttributeValue.id)
                    .where(ProductAttributeValueLinkModel.product_id == p.id)
                )
                val_result = await db.execute(val_stmt)
                for attr_name, value, uom in val_result.all():
                    attributes_dict[attr_name] = {
                        'value': value or '',
                        'unit': uom or ''
                    }
                    attribute_names.append(attr_name)
            except Exception:
                pass

            p_dict['attributes_dict'] = attributes_dict
            p_dict['attribute_names'] = attribute_names
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
                    attr_count += 1
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


def get_last_category_expr():
    return func.coalesce(
        func.nullif(Product.category_8, ''),
        func.nullif(Product.category_7, ''),
        func.nullif(Product.category_6, ''),
        func.nullif(Product.category_5, ''),
        func.nullif(Product.category_4, ''),
        func.nullif(Product.category_3, ''),
        func.nullif(Product.category_2, ''),
        func.nullif(Product.category_1, '')
    )


@router.get("/filters")
async def get_products_filters(
    project_id: str | None = None,
    brand_name: Optional[str] = None,
    category_1: Optional[str] = None,
    workflow_stage: str | None = None,
    db: AsyncSession = Depends(get_session),
):
    try:
        # 1. Define it FIRST
        last_cat_expr = get_last_category_expr()

        category_stmt = select(last_cat_expr).where(last_cat_expr.isnot(None))
        brand_stmt = select(Brand.name).join(
            Product, Product.brand_id == Brand.id).where(Brand.name.isnot(None))

        if project_id:
            category_stmt = category_stmt.join(
                ProjectProductLink, Product.id == ProjectProductLink.product_id
            ).where(ProjectProductLink.project_id == project_id)
            brand_stmt = brand_stmt.join(
                ProjectProductLink, Product.id == ProjectProductLink.product_id
            ).where(ProjectProductLink.project_id == project_id)
        if workflow_stage:
            category_stmt = category_stmt.where(
                Product.workflow_stage == workflow_stage)
            brand_stmt = brand_stmt.where(
                Product.workflow_stage == workflow_stage)
        if brand_name:
            category_stmt = category_stmt.where(
                Product.brand_name == brand_name)

        if category_1:
            brand_stmt = brand_stmt.where(last_cat_expr == category_1)

        category_result = await db.execute(category_stmt.distinct())
        category_rows = category_result.all()
        categories = sorted([row[0].strip()
                            for row in category_rows if row[0]])

        brand_result = await db.execute(brand_stmt.distinct())
        brand_rows = brand_result.all()
        brands = sorted([row[0].strip() for row in brand_rows if row[0]])

        return {"categories": categories, "brands": brands}
    except Exception as e:
        logger.error(
            f"Failed to fetch filters for project {project_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="Failed to fetch project filters")


@router.get("/attributes")
async def get_project_attributes(
        project_id: str = Query(..., description="Project ID"),
        category: str | None = Query(
            default=None, description="Optional category filter"),
        db: AsyncSession = Depends(get_session),):
    try:
        # stmt = select(Product.dynamic_attributes).where(
        #     ProjectProductLink.project_id == project_id)
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
            .join(ProjectProductLink, Product.id == ProjectProductLink.product_id) 
            .where(ProjectProductLink.project_id == project_id)
            .distinct()
        )
        if category:
            stmt = stmt.where(
                or_(
                    Product.category_1 == category,
                    Product.category_2 == category,
                    Product.category_3 == category,
                    Product.category_4 == category,
                    Product.category_5 == category,
                    Product.category_6 == category,
                    Product.category_7 == category,
                    Product.category_8 == category,
                    Product.taxonomy == category,
                )
            )
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
        
        stmt = select(Product).join(ProjectProductLink,Product.id==ProjectProductLink.product_id).where(
            ProjectProductLink.project_id == project_id)
        
        if brand_name:
            stmt = stmt.where(Product.brand_name == brand_name)
        
        # FIX: Check ALL category fields (category_1 through category_8 + taxonomy)
        if category_1:
            stmt = stmt.where(
                or_(
                    Product.category_1 == category_1,
                    Product.category_2 == category_1,
                    Product.category_3 == category_1,
                    Product.category_4 == category_1,
                    Product.category_5 == category_1,
                    Product.category_6 == category_1,
                    Product.category_7 == category_1,
                    Product.category_8 == category_1,
                    Product.taxonomy == category_1,
                )
            )
        
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
            ).join(ProjectProductLink,Product.id==ProjectProductLink.product_id).where(
                ProjectProductLink.project_id == project_id
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
            ProjectProductLink.project_id,
            func.count(Product.id).label('count')
        ).join(Product,Product.id==ProjectProductLink.project_id).where(
            and_(
                Product.workflow_stage == 'enrichment',
                Product.enrichment_status == 'pending'
            )
        ).group_by(ProjectProductLink.project_id, )

        result = await db.execute(stmt)
        counts = {str(row[0]): row[1] for row in result.all()}
        return counts
    except Exception as e:
        logger.error(f"Error getting enrichment counts: {str(e)}")
        return {}


@router.get("/categories", response_model=List[Dict[str, Any]])
async def get_categories(db: AsyncSession = Depends(get_session)):
    try:
        stmt = select(Category.id, Category.name).where(
            Category.is_active == True,
            Category.level == 1
        ).order_by(Category.name)
        result = await db.execute(stmt)
        return [{"id": str(row[0]), "name": row[1]} for row in result.all()]
    except Exception as e:
        logger.error(f"Failed to fetch categories: {e}")
        return []


@router.get("/brands", response_model=List[Dict[str, Any]])
async def get_brands(
    db: AsyncSession = Depends(get_session)
):
    try:
        stmt = select(Brand.id, Brand.name).where(
            Brand.is_active == True).order_by(Brand.name)
        result = await db.execute(stmt)
        brands = [{"id": str(row[0]), "name": row[1]} for row in result.all()]
        return brands
    except Exception as e:
        logger.error(f"Failed to fetch brands: {e}")
        return []


@router.post("/brands", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_brand(
    payload: BrandCreate,
    db: AsyncSession = Depends(get_session)
):
    try:
        normalized = payload.name.lower().strip()
        normalized_no_spaces = normalized.replace(" ", "")
        existing = await db.execute(
            select(Brand).where(
                func.lower(func.replace(Brand.name, " ", "")
                           ).like(normalized_no_spaces)
            )
        )
        if existing.scalars().first():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Brand already exists"
            )

        brand = Brand(
            name=payload.name,
            normalized_name=payload.name.lower().strip(),
            is_active=True,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        db.add(brand)
        await db.commit()
        await db.refresh(brand)

        return {"id": str(brand.id), "name": brand.name}

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to create brand: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create brand"
        )


@router.post("/categories", status_code=201)
async def create_category(
    payload: dict,
    db: AsyncSession = Depends(get_session)
):
    try:
        name = payload.get("name", "").strip()
        industry_id = payload.get("industry_id")

        if not name:
            raise HTTPException(
                status_code=400, detail="Category name is required")

        existing_stmt = select(Category).where(
            func.lower(Category.name) == func.lower(name)
        )

        if industry_id:
            existing_stmt = existing_stmt.where(
                Category.industry_id == UUID(industry_id))

        existing_result = await db.execute(existing_stmt)
        existing_category = existing_result.scalar_one_or_none()

        if existing_category:
            raise HTTPException(
                status_code=409,
                detail=f"Category '{name}' already exists"
            )
        cat = Category(
            name=payload["name"],
            industry_id=UUID(payload["industry_id"]) if payload.get(
                "industry_id") else None,
            level=payload.get("level", 1),
            full_path=payload.get("full_path", payload["name"]),
            is_active=True,
        )
        db.add(cat)
        await db.commit()
        await db.refresh(cat)
        return {"id": str(cat.id), "name": cat.name}
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/taxonomies")
async def get_all_taxonomies(db: AsyncSession = Depends(get_session)):
    try:
        stmt = select(Product.taxonomy).where(
            Product.taxonomy.isnot(None),
            Product.taxonomy != ""
        ).distinct()

        result = await db.execute(stmt)
        taxonomies = sorted([row[0] for row in result.all()])

        return taxonomies

    except Exception as e:
        print(f"Error fetching taxonomies: {e}")

        raise HTTPException(
            status_code=500,
            detail="Failed to fetch taxonomies"
        )
