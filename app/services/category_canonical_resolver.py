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
    """
    Purpose
    -------
    For a given category, return:
      1) winners: the canonical Attribute rows that should be treated as the *preferred*
         canonicals (i.e., excluding any attributes that are marked as aliases)
      2) alias_name_map: mapping from alias attribute *name* -> preferred canonical *name*

    Why
    ---
    Your DB can contain competing "canonical" attributes for the same concept (e.g.
    "Degree" and "Collation Angle"). Once you decide that "Degree" is an alias of
    "Collation Angle", you store that in category_attribute_aliases.

    This function then:
      - removes "Degree" from the winners list for that category
      - returns {"Degree": "Collation Angle"} so the pipeline can rewrite names.

    Behavior
    --------
    - If there are no category canonicals: returns ([], {})
    - If there are no alias rules for that category: winners == all canonicals, alias map == {}
    - If something fails: logs and returns (all canonicals, {}) as a safe fallback
    """

    # Safe fallback defaults
    all_attrs: List[Attribute] = []
    try:
        # ---------------------------------------------------------------------
        # 1) Load ALL canonical attributes linked to this category
        #    category_attributes: (category_id, attribute_id)
        #    attribute_master (Attribute): (id, attribute_name, unit, ...)
        # ---------------------------------------------------------------------
        stmt = (
            select(Attribute)
            .join(CategoryAttribute, CategoryAttribute.attribute_id == Attribute.id)
            .where(CategoryAttribute.category_id == category_id)
        )
        res = await db.execute(stmt)
        all_attrs = list(res.scalars().all())

        # No canonicals linked to this category → nothing to do
        if not all_attrs:
            logger.info(f"[CanonWinners] category_id={category_id} all=0 aliases=0 winners=0")
            return [], {}

        # ---------------------------------------------------------------------
        # 2) Load alias rules for this category and resolve IDs -> NAMES
        #
        # category_attribute_aliases stores:
        #   alias_attribute_id      -> canonical_attribute_id
        #
        # We join attribute_master twice:
        #   A_alias  gives alias attribute_name (e.g., "Degree")
        #   A_canon  gives canonical attribute_name (e.g., "Collation Angle")
        # ---------------------------------------------------------------------
        A_alias = aliased(Attribute)
        A_canon = aliased(Attribute)

        alias_stmt = (
            select(
                CategoryAttributeAlias.alias_attribute_id,
                CategoryAttributeAlias.canonical_attribute_id,
                A_alias.attribute_name,   # alias name
                A_canon.attribute_name,   # canonical/winner name
            )
            .join(A_alias, A_alias.id == CategoryAttributeAlias.alias_attribute_id)
            .join(A_canon, A_canon.id == CategoryAttributeAlias.canonical_attribute_id)
            .where(CategoryAttributeAlias.category_id == category_id)
        )

        alias_res = await db.execute(alias_stmt)
        alias_rows = alias_res.all()

        # Build:
        #  - alias_name_map: {"Degree": "Collation Angle"}
        #  - alias_ids: set({<attribute_id_of_Degree>, ...}) so we can exclude from winners
        alias_name_map: Dict[str, str] = {}
        alias_ids = set()

        for alias_id, canonical_id, alias_name, canonical_name in alias_rows:
            # ignore malformed rows
            if not alias_name or not canonical_name:
                continue
            if alias_name == canonical_name:
                continue

            alias_name_map[alias_name] = canonical_name
            alias_ids.add(alias_id)

        # ---------------------------------------------------------------------
        # 3) Winners = (all canonicals for category) minus (alias attributes)
        # ---------------------------------------------------------------------
        winners = [a for a in all_attrs if a.id not in alias_ids]

        logger.info(
            f"[CanonWinners] category_id={category_id} all={len(all_attrs)} "
            f"aliases={len(alias_rows)} winners={len(winners)}"
        )

        return winners, alias_name_map

    except Exception as e:
        # Production-safe fallback: do NOT break aggregation.
        # Return the full canonical list so the pipeline still has something to work with.
        logger.exception(f"[CanonWinners] Failed for category_id={category_id}: {e}")
        return all_attrs, {}