from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from app.models.pipeline import AuditTrail, Source
from app.core.database import get_session, async_session_factory
from app.models.product import Product

import logging

from app.aggregation import aggregate_product
from app.schemas.extraction import ExtractionRequest

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
            title = extracted_keys.get(
                'product_name') or extracted_keys.get('brand')

            result = aggregate_product(mpn=sku, title=title)
            if result.get('status') == 'success':
                statement = select(Product).where(Product.product_code == sku)
                prod_result = await db_session.execute(statement)
                product = prod_result.scalars().first()
                if product:
                    product.attributes = result['golden_record']['attributes']
                    product.enrichment_status = 'completed'
                    db_session.add(product)
                    await db_session.commit()
            source.status = "completed"
            db_session.add(source)

            audit = AuditTrail(
                product_id=sku or "unknown",
                stage="extraction",
                attribute_name="ingestion",
                selected_value="Success",
                sources_used=source.source_url if source.source_url else "Manual Input",
                reason=f"AI successfully parsed manual input for {sku}"
            )
            db_session.add(audit)

            await db_session.commit()
            logger.info(f"Successfully processed source {source_id}")

        except Exception as e:
            await db_session.rollback()
            logger.error(
                f"Background extraction failed for {source_id}: {str(e)}")
            source = await db_session.get(Source, source_id)
            if source:
                source.status = "failed"
                db_session.add(source)
                await db_session.commit()


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
