import hashlib
import logging
import asyncio
from typing import List, Dict, Tuple
from uuid import UUID
import numpy as np
from app.utils.remapping import get_embedding_model
from scipy.spatial.distance import cosine
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert
from app.models.attribute import Attribute, CategoryAttribute
from app.models.category_alias_job import CategoryAliasJob
from app.models.category_attribute_alias import CategoryAttributeAlias
from app.llm import call_llm_with_schema
from app.schemas.canonical import CanonicalAliasResponse
logger = logging.getLogger("canonical_alias_service")
_CANDIDATE_SIM_THRESHOLD = 0.85   
_LLM_CONFIDENCE_THRESHOLD = 0.70  
def _compute_canonical_fingerprint(rows: List[Tuple[str, str]]) -> str:
    try:
        payload = ",".join([f"{name}|{unit or ''}" for name, unit in sorted(rows)])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
    except Exception as e:
        logger.warning(f"[AliasService] fingerprint failed: {e}")
        return hashlib.sha256(str(len(rows)).encode("utf-8")).hexdigest()
async def enqueue_category_alias_job(category_id: UUID, db: AsyncSession) -> None:
    try:
        stmt = (
            select(Attribute.attribute_name, Attribute.unit)
            .join(CategoryAttribute, CategoryAttribute.attribute_id == Attribute.id)
            .where(CategoryAttribute.category_id == category_id)
        )
        res = await db.execute(stmt)
        rows = [(r[0], r[1]) for r in res.all() if r[0]]
        if not rows:
            return
        current_fp = _compute_canonical_fingerprint(rows)
        job_stmt = select(CategoryAliasJob).where(
            CategoryAttributeAlias.category_id == category_id
        ) if False else select(CategoryAliasJob).where(
            CategoryAliasJob.category_id == category_id
        )
        existing = (await db.execute(job_stmt)).scalars().first()
        if existing and existing.fingerprint == current_fp and existing.status in ("completed", "pending"):
            return  
        if not existing:
            db.add(CategoryAliasJob(
                category_id=category_id,
                fingerprint=current_fp,
                status="pending",
            ))
        else:
            existing.status = "pending"
            existing.fingerprint = current_fp
            existing.attempts = 0
            existing.last_error = None
        await db.commit()
        logger.info(f"[AliasService] Enqueued alias resolution for category_id={category_id}")
    except Exception as e:
        try:
            await db.rollback()
        except Exception:
            pass
        logger.warning(f"[AliasService] enqueue failed for category_id={category_id}: {e}")
async def process_category_alias_resolution(
    category_id: UUID,
    db: AsyncSession,
    llm_provider: str,
) -> Dict[str, int]:
    stats = {"candidates": 0, "merged": 0, "skipped": 0}
    try:
        try:
            stmt = (
                select(Attribute)
                .join(CategoryAttribute, CategoryAttribute.attribute_id == Attribute.id)
                .where(CategoryAttribute.category_id == category_id)
            )
            res = await db.execute(stmt)
            attrs = list(res.scalars().all())
        except Exception as e:
            logger.warning(f"[AliasService] load canonicals failed for {category_id}: {e}")
            return stats
        if len(attrs) < 2:
            return stats
        name_to_id = {a.attribute_name: a.id for a in attrs if a.attribute_name}
        name_to_unit = {a.attribute_name: a.unit for a in attrs if a.attribute_name}
        names = list(name_to_id.keys())
        if not names:
            return stats
        try:
            model = await get_embedding_model()
            embeddings = await asyncio.to_thread(model.encode, names)
        except Exception as e:
            logger.warning(f"[AliasService] embedding model unavailable for {category_id}: {e}")
            return stats
        try:
            candidates: List[List[str]] = []
            used = set()
            for i, n1 in enumerate(names):
                if n1 in used:
                    continue
                cluster = [n1]
                used.add(n1)
                for j, n2 in enumerate(names):
                    if j <= i or n2 in used:
                        continue
                    sim = 1 - cosine(embeddings[i], embeddings[j])
                    if sim >= _CANDIDATE_SIM_THRESHOLD:
                        cluster.append(n2)
                        used.add(n2)
                if len(cluster) > 1:
                    candidates.append(cluster)
            stats["candidates"] = len(candidates)
        except Exception as e:
            logger.warning(f"[AliasService] clustering failed for {category_id}: {e}")
            return stats
        if not candidates:
            return stats
        try:
            groups_text = "\n".join(
                f"Group {i+1}: {g}\n  Units: "
                + ", ".join([f"{n}={name_to_unit.get(n)}" for n in g])
                for i, g in enumerate(candidates)
            )
            prompt = f"""
                You are an expert taxonomist classifying product attribute names into semantic equivalence groups.
                For each candidate group below, decide: do these names refer to the exact same
                underlying measurable property, just phrased differently? Or are they distinct
                properties that merely sound or look similar?
                Reason from first principles about what each name actually measures or describes.
                Two names are equivalent only if a domain expert would consider them interchangeable
                labels for one spec field — not merely related, adjacent, or co-occurring.
                Test: could you delete one name and lose zero information, because the other name
                already captures it exactly? If deleting one loses information the other lacks,
                they are NOT equivalent — reject, even if both relate to the same general topic.
                "preferred" must be selected from the group's own names — you are choosing the best
                existing label, not naming a new concept.
                Candidate groups:
                {groups_text}
                Return strict JSON matching this exact shape, one object per group you accept:
                {{
                "decisions": [
                    {{
                    "aliases": ["Name A", "Name B"],
                    "preferred": "Name A",
                    "confidence": 0.9,
                    "reason": "short explanation"
                    }}
                ]
                }}
                Field rules:
                - "aliases": ALL names in the group being merged, including the preferred one.
                - "preferred": must be one of the strings already in "aliases".
                - "confidence": a number between 0 and 1.
                - "reason": one sentence.
                - Omit a group entirely from "decisions" if you reject the merge (confidence < 0.75) —
                do not include rejected groups with a low confidence, just leave them out.
                - Do NOT use any other field names (no "names", "decision", "is_equivalent", "reasoning").
                """
        except Exception as e:
            logger.warning(f"[AliasService] prompt build failed for {category_id}: {e}")
            return stats
        try:
            result = await call_llm_with_schema(
                prompt=prompt,
                response_model="CanonicalAliasResponse",
                llm_provider="gemini",
                estimated_tokens=500 + len(candidates) * 80,
            )
        except Exception as e:
            logger.warning(f"[AliasService] LLM call failed for {category_id}: {e}")
            return stats
        if not result or not getattr(result, "decisions", None):
            return stats
        try:
            name_to_group: Dict[str, List[str]] = {}
            for g in candidates:
                for n in g:
                    name_to_group[n] = g
            rows_to_upsert = []
            for d in result.decisions:
                preferred = (d.preferred or "").strip()
                if getattr(d, "confidence", 0) < _LLM_CONFIDENCE_THRESHOLD:
                    stats["skipped"] += 1
                    continue
                if preferred not in name_to_id:
                    stats["skipped"] += 1
                    continue
                if preferred not in name_to_group:
                    stats["skipped"] += 1
                    continue
                canonical_id = name_to_id[preferred]
                canonical_group = name_to_group[preferred]
                for alias in (d.aliases or []):
                    if alias == preferred:
                        continue
                    if alias not in name_to_id:
                        continue
                    if name_to_group.get(alias) != canonical_group:
                        continue
                    rows_to_upsert.append({
                        "category_id": category_id,
                        "alias_attribute_id": name_to_id[alias],
                        "canonical_attribute_id": canonical_id,
                        "confidence": d.confidence,
                        "reason": d.reason or "LLM resolved",
                    })
                    stats["merged"] += 1
            if not rows_to_upsert:
                return stats
            try:
                delete_stmt = CategoryAttributeAlias.__table__.delete().where(
                    CategoryAttributeAlias.category_id == category_id
                )
                await db.execute(delete_stmt)
            except Exception as e:
                logger.warning(f"[AliasService] delete old aliases failed for {category_id}: {e}")
            stmt = pg_insert(CategoryAttributeAlias).values(rows_to_upsert)
            stmt = stmt.on_conflict_do_update(
                index_elements=["category_id", "alias_attribute_id"],
                set_={
                    "canonical_attribute_id": stmt.excluded.canonical_attribute_id,
                    "confidence": stmt.excluded.confidence,
                    "reason": stmt.excluded.reason,
                },
            )
            await db.execute(stmt)
            await db.commit()
        except Exception as e:
            try:
                await db.rollback()
            except Exception:
                pass
            logger.warning(f"[AliasService] persist aliases failed for {category_id}: {e}")
            return stats
        logger.info(
            f"[AliasService] category_id={category_id} candidates={stats['candidates']} "
            f"merged={stats['merged']} skipped={stats['skipped']}"
        )
        return stats
    except Exception as e:
        logger.exception(f"[AliasService] process_category_alias_resolution crashed: {e}")
        return stats