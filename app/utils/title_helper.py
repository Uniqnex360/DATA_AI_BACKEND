

import logging
from typing import Dict, Any, Optional

from app.models.product import Product
from app.utils.title_recommendation import generate_title_recommendation
from app.utils.timezone import now_ist

logger = logging.getLogger("title_helper")


def extract_title_result(title_rec: Any) -> tuple[Optional[str], float]:
    
    if title_rec is None:
        return None, 0.0

    if isinstance(title_rec, dict):
        return (
            title_rec.get("recommended_title"),
            title_rec.get("confidence", 0.0),
        )

    return (
        getattr(title_rec, "recommended_title", None),
        getattr(title_rec, "confidence", 0.0),
    )


async def apply_title_recommendation(
    product: Product,
    attributes: Dict[str, Any],
    llm_provider: str = "openai",
    force: bool = False,
) -> None:
    
    if not force and product.title_recommendation:
        logger.info(
            f"Skipping title generation for {product.product_code}: title already exists"
        )
        return

    try:
        title_rec = await generate_title_recommendation(
            brand=product.brand_name or "",
            attributes=attributes or {},
            taxonomy=product.taxonomy or "",
            llm_provider=llm_provider,
        )

        recommended_title, confidence = extract_title_result(title_rec)

        if recommended_title:
            product.title_recommendation = recommended_title
            product.title_confidence = confidence
            product.title_generated_at = now_ist()
            logger.info(
                f"✓ Title generated for {product.product_code}: "
                f"'{recommended_title}' (confidence={confidence})"
            )
        else:
            logger.warning(
                f"Title generation returned empty result for {product.product_code}"
            )

    except Exception as e:
        logger.warning(
            f"Title recommendation failed for {product.product_code}: {e}"
        )
