from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select
import logging
from app.aggregation.worker_pool import get_worker_pool
from app.auth.dependencies import get_current_user
from app.core.database import get_session
from app.models.pdf_validation import PdfValidation
from app.models.product import Product
from app.models.user import User
from app.schemas.pdf_extraction import PdfValidationDecision
from app.utils.image_validator import validate_image_url
from app.utils.timezone import now_ist
logger=logging.getLogger(__name__)
router=APIRouter()
@router.get("/pending")
async def list_pending_pdf_validations(product_code: Optional[str] = None,project_id:Optional[str]=None,db:AsyncSession=Depends(get_session)):
    try:
        stmt=select(PdfValidation).where(PdfValidation.status=='pending')
        if product_code:
            stmt=stmt.where(PdfValidation.product_code==product_code)
        if project_id:
            stmt=stmt.where(PdfValidation.project_id==project_id)
        stmt=stmt.order_by(PdfValidation.created_at.desc())
        result=await db.execute(stmt)
        validations=result.scalars().all()
        return [
            {
            "id": str(v.id),
            "product_code": v.product_code,
            "project_id": str(v.project_id) if v.project_id else None,
            "pdf_url": v.pdf_url,
            "source_page_url": v.source_page_url,
            "status": v.status,
            "created_at": v.created_at.isoformat() if v.created_at else None,
        }
        for v in validations
        ]
    except Exception as e:
        logger.warning(f"Product discovery failed: {e}")
        raise e
from app.aggregation.aggregate_product import extract_approved_pdf
from sqlalchemy.orm.attributes import flag_modified

@router.post("/{validation_id}/resolve")
async def resolve_pdf_validation(
    validation_id: str,
    payload: PdfValidationDecision,
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    if payload.decision not in ("approved", "rejected"):
        raise HTTPException(400, "decision must be 'approved' or 'rejected'")

    v = await db.get(PdfValidation, validation_id)
    if not v:
        raise HTTPException(404, "Validation not found")
    if v.status != "pending":
        raise HTTPException(409, f"Already resolved: {v.status}")

    v.status = payload.decision
    v.resolved_at = now_ist()
    v.resolved_by = current_user.id
    db.add(v)
    await db.commit()

    if payload.decision == "approved":
        result = await extract_approved_pdf(v, db, llm_provider="openai")
        if result.get("status") == "success":
            product = result["product"]
            for attr in result["attributes"]:
                product.attributes[attr["name"]] = attr
            flag_modified(product, "attributes")
            sources = list(product.sources_consulted or [])
            if v.pdf_url not in sources:
                sources.append(v.pdf_url)
            product.sources_consulted = sources
            flag_modified(product, "sources_consulted")
            found_images = result.get("image_urls") or []
            slot = next((i for i in range(1, 9) if not getattr(product, f"image_url_{i}", None)), None)
            for img_url in found_images:
                if slot is None or slot > 8:
                    break
                if await validate_image_url(img_url):
                    setattr(product, f"image_url_{slot}", img_url)
                    slot += 1

            db.add(product)
            await db.commit()
            logger.info(f"✓ Extracted {len(result['attributes'])} attrs from approved PDF for {v.product_code}")
        else:
            logger.warning(f"Approved PDF extraction failed for {v.product_code}: {result.get('reason')}")

    return {"status": "ok", "validation_id": str(v.id), "decision": payload.decision}