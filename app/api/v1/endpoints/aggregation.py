from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, func 
from app.core.database import get_session
from app.models.product import Product
from app.models.pipeline import RawExtraction
import logging
logger = logging.getLogger("aggregation_router")
router = APIRouter()
@router.get("/attributes/{product_id}")
async def get_aggregated_attributes(product_id: str, db: AsyncSession = Depends(get_session)):
    try:
        product = await db.get(Product, product_id)
        if not product:
            raise HTTPException(status_code=404, detail="Product not found")
        statement = select(RawExtraction).where(
            func.json_extract_path_text(RawExtraction.product_keys, 'sku') == product.product_code
        )
        result = await db.execute(statement)
        extractions = result.scalars().all()
        evidence_map = {}
        for ext in extractions:
            for attr_name, attr_val in ext.raw_attributes.items():
                if attr_name not in evidence_map:
                    evidence_map[attr_name] = []
                evidence_map[attr_name].append({
                    "value": str(attr_val),
                    "confidence": ext.confidence,
                    "source_id": str(ext.source_id)[:8] 
                })
        ui_format = []
        current_attrs = product.attributes or {}
        for attr_name, master_value in current_attrs.items():
            values_from_sources = evidence_map.get(attr_name, [])
            unique_values = set([v["value"] for v in values_from_sources])
            has_conflict = len(unique_values) > 1
            ui_format.append({
                "id": f"{product_id}_{attr_name}",
                "product_id": product_id,
                "attribute_name": attr_name,
                "has_conflict": has_conflict,
                "values": values_from_sources if values_from_sources else [
                    {"value": str(master_value), "confidence": 1.0, "source_id": "Truth Engine"}
                ]
            })
        return ui_format
    except Exception as e:
        logger.error(f"CRITICAL: Aggregation UI Fetch failed: {str(e)}")
        return []