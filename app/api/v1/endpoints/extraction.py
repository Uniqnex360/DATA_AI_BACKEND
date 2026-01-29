from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from app.models.pipeline import AuditTrail, CleansingIssue, RawExtraction, Source, SourcePriority
from app.core.database import get_session, async_session_factory
from app.models.product import Product
from typing import List
import logging
from app.aggregation import aggregate_product
from app.schemas.extraction import ExtractionRequest, SourceMetricsResponse
from app.schemas.pipeline import SourcePriorityResponse
from app.utils import is_invalid
logger = logging.getLogger("extraction_router")
router = APIRouter()
@router.get("/")
async def getAllSources(db: AsyncSession = Depends(get_session)):
    try:
        statement = select(Source).order_by(Source.uploaded_at.desc())
        result = await db.execute(statement)
        sources = result.scalars().all()
        return sources
    except Exception as e:
        logger.error(f"Failed to fetch sources: {str(e)}")
        raise HTTPException(
            status_code=500, detail="Could not retrieve import history")
        
async def run_extraction_task(source_id: str, content: str):
    async with async_session_factory() as db_session:
        try:
            source = await db_session.get(Source, source_id)
            if not source:
                return

            lines = content.split('\n')
            extracted_keys = {}
            for line in lines:
                if ':' in line:
                    k, v = line.split(':', 1)
                    extracted_keys[k.strip().lower()] = v.strip()

            sku = extracted_keys.get('sku') or extracted_keys.get('mpn')
            title = extracted_keys.get('product_name') or extracted_keys.get('brand')

            result = aggregate_product(mpn=sku, title=title)
            
            if result.get('status') == 'success' and sku:
                ai_data = result.get('golden_record', {}).get('attributes', {})
                
                for attr_name, attr_value in ai_data.items():
                    val_str = str(attr_value)
                    
                    if is_invalid(val_str):
                        db_session.add(CleansingIssue(
                            product_id=sku,
                            attribute_name=attr_name,
                            issue_type="invalid",
                            details=f"Placeholder detected: '{val_str}'",
                            resolved=False
                        ))
                    
                    if "price" in attr_name.lower():
                        try:
                            price_val = float(val_str.replace('$', '').replace(',', '').strip())
                            if price_val <= 0:
                                db_session.add(CleansingIssue(
                                    product_id=sku,
                                    attribute_name=attr_name,
                                    issue_type='invalid',
                                    details='Price must be a positive number',
                                    resolved=False
                                ))
                        except (ValueError, TypeError):
                            db_session.add(CleansingIssue(
                                product_id=sku,
                                attribute_name=attr_name,
                                issue_type='invalid',
                                details=f"Non-numeric price found: {val_str}",
                                resolved=False
                            ))

                statement = select(Product).where(Product.product_code == sku)
                prod_result = await db_session.execute(statement)
                product = prod_result.scalars().first()

                if not product:
                    logger.info(f"Creating new MASTER record: {sku}")
                    product = Product(
                        product_code=sku,
                        product_name=title or sku,
                        brand_name=extracted_keys.get('brand'),
                        attributes=ai_data,
                        enrichment_status='completed',
                        completeness_score=min(len(ai_data) * 5, 100)
                    )
                else:
                    logger.info(f"Updating existing master record: {sku}")
                    product.attributes = ai_data
                    product.enrichment_status = 'completed'
                    product.completeness_score = min(len(ai_data) * 5, 100)
                
                db_session.add(product)

            source.status = "completed"
            db_session.add(source)

            db_session.add(AuditTrail(
                product_id=sku or "unknown",
                stage="extraction",
                attribute_name="ingestion",
                selected_value="Success",
                sources_used=source.source_url if source.source_url else "Manual Input",
                reason=f"AI successfully parsed manual input for {sku}"
            ))

            current_project_id = source.project_id or "default-project"
            prio_stmt = select(SourcePriority).where(SourcePriority.source_id == str(source.id))
            prio_check = await db_session.execute(prio_stmt)
            
            if not prio_check.scalars().first():
                db_session.add(SourcePriority(
                    project_id=str(current_project_id),
                    source_id=str(source.id),
                    priority_rank=1,
                    reliability_score=0.9 if source.source_type == 'pdf' else 0.7
                ))

            await db_session.commit()
            logger.info(f"✓ Full pipeline success for {source_id}")

        except Exception as e:
            await db_session.rollback()
            logger.error(f"Pipeline crashed for {source_id}: {str(e)}")
            try:
                async with async_session_factory() as error_session:
                    fail_source = await error_session.get(Source, source_id)
                    if fail_source:
                        fail_source.status = "failed"
                        error_session.add(fail_source)
                        await error_session.commit()
            except: pass          
             
@router.post("/", status_code=status.HTTP_202_ACCEPTED)
async def extract_from_source(
    payload: ExtractionRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session)
):
    try:
        new_source = Source(
            source_type=payload.sourceType,
            source_url=payload.sourceUrl,
            project_id=payload.projectId,
            status="processing",
            source_metadata={"raw_length": len(payload.content)}
        )
        db.add(new_source)
        await db.commit()
        await db.refresh(new_source)
        background_tasks.add_task(
            run_extraction_task,
            str(new_source.id),
            payload.content,
        )
        return {
            "status": "accepted",
            "source_id": str(new_source.id),
            "message": "AI pipeline initialized in background"
        }
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to initialize extraction: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="System failed to initialize the extraction pipeline"
        )
@router.get("/priorities/{project_id}", response_model=List[SourcePriorityResponse],status_code=status.HTTP_200_OK)
async def get_project_priorities(project_id: str, db: AsyncSession = Depends(get_session)):
    try:
        statement = (
            select(SourcePriority)
            .where(SourcePriority.project_id == project_id)
            .order_by(SourcePriority.priority_rank.asc())
        )
        result = await db.execute(statement)
        priorities = result.scalars().all()
        logger.info(f"Retrieved {len(priorities)} priority rankings for project {project_id}")
        return priorities
    except Exception as e:
        logger.error(f"DATABASE ERROR in get_project_priorities: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal system error while retrieving source rankings"
        )
@router.get("/{source_id}/metrics", response_model=SourceMetricsResponse)
async def get_source_metrics(source_id: str, db: AsyncSession = Depends(get_session)):
    try:
        source = await db.get(Source, source_id)
        if not source:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, 
                detail="Source record not found"
            )
        statement = select(RawExtraction).where(RawExtraction.source_id == source_id)
        result = await db.execute(statement)
        extractions = result.scalars().all()
        if not extractions:
            return {
                "avgConfidence": 0.0,
                "completeness": 0.0,
                "totalAttributes": 0
            }
        total_conf = sum(ext.confidence for ext in extractions)
        avg_conf = total_conf / len(extractions)
        unique_attributes = set()
        for ext in extractions:
            if isinstance(ext.raw_attributes, dict):
                unique_attributes.update(ext.raw_attributes.keys())
        total_attrs_count = len(unique_attributes)
        TARGET_ATTR_COUNT = 20 
        completeness_score = min(total_attrs_count / TARGET_ATTR_COUNT, 1.0)
        logger.info(f"Metrics generated for source {source_id}: {total_attrs_count} attrs found")
        return {
            "avgConfidence": round(avg_conf, 2),
            "completeness": round(completeness_score, 2),
            "totalAttributes": total_attrs_count
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"CRITICAL ERROR calculating metrics for {source_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Analytics engine failed to calculate metrics"
        )