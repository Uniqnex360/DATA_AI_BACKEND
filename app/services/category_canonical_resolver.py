import logging
from typing import Dict, List, Tuple
from uuid import UUID
from sqlalchemy.orm import aliased
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from app.models.attribute import Attribute, CategoryAttribute
from app.models.category_attribute_alias import CategoryAttributeAlias
logger = logging.getLogger("category_canonical_resolver")
async def load_category_canonical_winners(
    category_id: UUID,
    db: AsyncSession,
) -> Tuple[List[Attribute], Dict[str, str]]:
    all_attrs: List[Attribute] = []
    try:
        stmt = (
            select(Attribute)
            .join(CategoryAttribute, CategoryAttribute.attribute_id == Attribute.id)
            .where(CategoryAttribute.category_id == category_id)
        )
        res = await db.execute(stmt)
        all_attrs = list(res.scalars().all())
        if not all_attrs:
            logger.info(f"[CanonWinners] category_id={category_id} all=0 aliases=0 winners=0")
            return [], {}
        A_alias = aliased(Attribute)
        A_canon = aliased(Attribute)
        alias_stmt = (
            select(
                CategoryAttributeAlias.alias_attribute_id,
                CategoryAttributeAlias.canonical_attribute_id,
                A_alias.attribute_name,   
                A_canon.attribute_name,   
            )
            .join(A_alias, A_alias.id == CategoryAttributeAlias.alias_attribute_id)
            .join(A_canon, A_canon.id == CategoryAttributeAlias.canonical_attribute_id)
            .where(CategoryAttributeAlias.category_id == category_id)
        )
        alias_res = await db.execute(alias_stmt)
        alias_rows = alias_res.all()
        alias_name_map: Dict[str, str] = {}
        alias_ids = set()
        for alias_id, canonical_id, alias_name, canonical_name in alias_rows:
            if not alias_name or not canonical_name:
                continue
            if alias_name == canonical_name:
                continue
            alias_name_map[alias_name] = canonical_name
            alias_ids.add(alias_id)
        winners = [a for a in all_attrs if a.id not in alias_ids]
        logger.info(
            f"[CanonWinners] category_id={category_id} all={len(all_attrs)} "
            f"aliases={len(alias_rows)} winners={len(winners)}"
        )
        return winners, alias_name_map
    except Exception as e:
        logger.exception(f"[CanonWinners] Failed for category_id={category_id}: {e}")
        return all_attrs, {}