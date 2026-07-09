import asyncio
import logging
from typing import List

from sqlalchemy import text
from sqlmodel import Session
from app.models.product import Product
from app.models.user import User 
from app.models.project import Project
from app.models.attribute import Attribute, AttributeValue
from app.models.category import Category
from app.models.category_attribute_alias import CategoryAttributeAlias
from app.models.category_canonical_state import CategoryCanonicalState
from app.core.database import engine, async_session_factory  # adjust to your project
from app.services.canonical_alias_service import (
    enqueue_category_alias_job,
    process_category_alias_resolution,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("prewarm_aliases")

async def get_categories_with_products() -> List[str]:
    """Return all category_ids that have at least one linked product attribute."""
    sql = text("""
        SELECT DISTINCT ca.category_id::text
        FROM category_attributes ca
        WHERE EXISTS (
            SELECT 1 FROM product_attribute_link pal
            WHERE pal.attribute_id = ca.attribute_id
        )
    """)
    async with async_session_factory() as session:
        result = await session.execute(sql)
        rows = result.all()
    return [r[0] for r in rows]


async def main():
    llm_provider = "openai"
    category_ids = await get_categories_with_products()
    logger.info(f"[Prewarm] Found {len(category_ids)} categories to pre-warm")

    completed = 0
    failed = 0
    for idx, category_id in enumerate(category_ids, start=1):
        try:
            async with async_session_factory() as db:
                await enqueue_category_alias_job(category_id, db)
                stats = await process_category_alias_resolution(
                    category_id, db, llm_provider=llm_provider
                )
                logger.info(
                    f"[Prewarm] ({idx}/{len(category_ids)}) "
                    f"category_id={category_id} stats={stats}"
                )
                completed += 1
        except Exception as e:
            logger.exception(f"[Prewarm] category_id={category_id} failed: {e}")
            failed += 1

    logger.info(f"[Prewarm] Done. completed={completed} failed={failed}")


if __name__ == "__main__":
    asyncio.run(main())