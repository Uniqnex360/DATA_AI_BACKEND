import asyncio
import logging
import re
from typing import Dict, List, Optional
from uuid import UUID
import numpy as np
from scipy.spatial.distance import cosine
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from app.llm import call_llm_with_schema
from app.models.attribute import Attribute, AttributeValue, CategoryAttribute
from app.models.product_attribute_link import ProductAttributeValueLinkModel
logger = logging.getLogger("cluster_attributes_by_meaning")
_embedding_model = None
_db_attr_cache: Dict[UUID, Dict] = {}
_CONTAINER_TERMS = {
    'carton', 'pack', 'pallet', 'box', 'case', 'each', 'unit', 'piece', 'bag', 'sleeve'
}
AUTO_MERGE_THRESHOLD = 0.92
LLM_CONFIRM_THRESHOLD = 0.55
LOCAL_CLUSTER_THRESHOLD = 0.85


def _extract_container_term(name: str) -> str:
    words = re.findall(r"\b[a-zA-Z]+\b", name.lower())
    found = [w for w in words if w in _CONTAINER_TERMS]
    return found[0] if len(found) == 1 else ""


def _should_skip_container_clustering(name_a: str, name_b: str) -> bool:
    term_a = _extract_container_term(name_a)
    term_b = _extract_container_term(name_b)
    if term_a and term_b and term_a != term_b:
        return True
    return False


async def get_embedding_model():

    global _embedding_model
    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading embedding model (first call, will be cached)")
        _embedding_model = await asyncio.to_thread(
            SentenceTransformer, 'all-MiniLM-L6-v2', device='cpu'
        )
    return _embedding_model


async def get_category_canonical_attributes(
    category_id: UUID,
    db: AsyncSession,
    sample_values_limit: int = 5,
) -> List[Dict]:

    cache_key = category_id
    if cache_key in _db_attr_cache:
        logger.info(
            f"[CategoryCanonicals] cache_hit category_id={category_id} "
            f"count={len(_db_attr_cache[cache_key]['meta'])}"
        )
        return _db_attr_cache[cache_key]["meta"]
    stmt = (
        select(Attribute.id, Attribute.attribute_name, Attribute.unit)
        .join(CategoryAttribute, CategoryAttribute.attribute_id == Attribute.id)
        .where(CategoryAttribute.category_id == category_id)
    )
    result = await db.execute(stmt)
    rows = result.all()
    logger.info(
        f"[CategoryCanonicals] db_load category_id={category_id} rows={len(rows)}")
    logger.info(
        f"[CategoryCanonicals] db_names_units={[(name, unit) for _, name, unit in rows][:120]}")
    meta = []
    for attr_id, name, unit in rows:
        val_stmt = (
            select(AttributeValue.value)
            .where(AttributeValue.attribute_id == attr_id)
            .where(AttributeValue.value.isnot(None))
            .limit(sample_values_limit)
        )
        val_result = await db.execute(val_stmt)
        sample_values = [v for (v,) in val_result.all() if v]
        meta.append({
            "name": name,
            "unit": unit,
            "sample_values": sample_values,
        })
    logger.info(
        f"[CategoryCanonicals] meta_built category_id={category_id} meta_count={len(meta)}")
    logger.info(
        f"[CategoryCanonicals] meta_names_sample={[(m['name'], m.get('unit')) for m in meta[:60]]}")
    model = await get_embedding_model()
    names = [m["name"] for m in meta]
    embeddings = await asyncio.to_thread(model.encode, names) if names else np.array([])
    _db_attr_cache[cache_key] = {"names": names,
                                 "embeddings": embeddings, "meta": meta}
    return meta


def invalidate_category_cache(category_id: UUID) -> None:
    _db_attr_cache.pop(category_id, None)


async def _llm_confirm_batch(
    ambiguous: List[Dict],
    llm_provider: str,
) -> Dict[str, Optional[str]]:

    if not ambiguous:
        return {}
    lines = []
    for item in ambiguous:
        cand_lines = "\n".join(
            f'    - "{c["name"]}" (unit: {c["unit"] or "none"}, typical values seen: {c["sample_values"]})'
            for c in item["candidates"]
        )
        lines.append(
            f'New attribute: "{item["raw_name"]}" = {item["raw_value"]} {item["raw_unit"] or ""}\n'
            f'  Candidate DB canonical attributes:\n{cand_lines}'
        )
    prompt = f"""
You are matching newly scraped product attributes against an existing DB of
canonical attribute names for the SAME product category.
For each new attribute below, decide if it represents the SAME physical
specification as one of its candidate DB attributes.
Rules:
- Value match strengthens confidence but is NOT required (different products
  legitimately have different values for the same spec).
- If the new attribute and a candidate are semantically the same concept
  worded differently (abbreviation, case, spelling, word order, synonym) -> match.
- If they are genuinely different facts that happen to share similar wording
  (e.g. "Net Weight" vs "Gross Weight", "No-Load Speed" vs "Rated Load Speed")
  -> do NOT match, even if current values coincide.
- Use "typical values seen" for each candidate to help distinguish real
  duplicates from coincidental value overlap.
Items to evaluate:
{chr(10).join(lines)}
Return strict JSON:
{{
  "matches": [
    {{"raw_name": "...", "matched_canonical": "exact candidate name or null", "confidence": 0.0-1.0}}
  ]
}}
"""
    try:
        result = await call_llm_with_schema(
            prompt=prompt,
            response_model="BatchCanonicalMatchResponse",
            llm_provider=llm_provider,
            estimated_tokens=800 + len(ambiguous) * 150,
        )
    except Exception as e:
        logger.warning(f"LLM canonical match batch failed: {e}")
        return {}
    mapping = {}
    if result and hasattr(result, "matches"):
        for m in result.matches:
            if m.matched_canonical and m.confidence > 0.85:
                mapping[m.raw_name] = m.matched_canonical
            else:
                mapping[m.raw_name] = None
    return mapping


async def _value_collision_check(
    raw_attrs: List[Dict],
    canonical_map: Dict[str, str],
    llm_provider: str = "openai",
) -> Dict[str, str]:
    from collections import defaultdict
    groups = defaultdict(set)
    for a in raw_attrs:
        mapped_name = canonical_map.get(a['name'], a['name'])
        val = str(a.get('value', '')).strip().lower()
        unit = str(a.get('unit') or '').strip().lower()
        if not val or not unit:
            continue
        groups[(val, unit)].add(mapped_name)
    collision_batch = []
    for (val, unit), names in groups.items():
        if len(names) > 1:
            names_list = sorted(names)
            logger.warning(
                f"[ValueCollision] value='{val}' unit='{unit}' names={names_list}")
            collision_batch.append({
                "raw_name": names_list[0],
                "raw_value": val,
                "raw_unit": unit,
                "candidates": [
                    {"name": n, "unit": unit, "sample_values": [val]}
                    for n in names_list[1:]
                ],
            })
    if not collision_batch:
        return canonical_map
    llm_results = await _llm_confirm_batch(collision_batch, llm_provider)
    logger.info(f"[ValueCollision] llm_results={llm_results}")
    for raw_name, matched in llm_results.items():
        if matched:
            for k, v in list(canonical_map.items()):
                if v == raw_name:
                    canonical_map[k] = matched
            canonical_map[raw_name] = matched
            logger.info(f"[ValueCollision] merged '{raw_name}' -> '{matched}'")
    return canonical_map


async def cluster_attributes_by_meaning(
    raw_attrs: List[Dict],
    category_id: Optional[UUID] = None,
    db: Optional[AsyncSession] = None,
    canonical_names: List[str] = None,
    llm_provider: str = "openai",
    threshold: float = LOCAL_CLUSTER_THRESHOLD,
) -> List[Dict]:
    if not raw_attrs:
        return raw_attrs
    model = await get_embedding_model()
    unique_by_name: Dict[str, Dict] = {a['name']: a for a in raw_attrs}
    attr_names = list(unique_by_name.keys())
    if len(attr_names) < 2:
        return raw_attrs
    canonical_map: Dict[str, str] = {}
    unresolved = list(attr_names)
    if category_id and db:
        db_meta = await get_category_canonical_attributes(category_id, db)
        if db_meta:
            db_names = [m["name"] for m in db_meta]
            db_embeddings = _db_attr_cache[category_id]["embeddings"]
            raw_embeddings = await asyncio.to_thread(model.encode, attr_names)
            llm_batch = []
            still_unresolved = []
            for i, name in enumerate(attr_names):
                sims = [1 - cosine(raw_embeddings[i], db_embeddings[j])
                        for j in range(len(db_names))]
                ranked = sorted(zip(db_meta, sims), key=lambda x: -x[1])
                best_meta, best_sim = ranked[0] if ranked else (None, 0.0)
                logger.info(
                    f"[DBMatch] raw='{name}' best='{best_meta['name'] if best_meta else None}' "
                    f"sim={best_sim:.3f} top3={[(m['name'], round(s, 3)) for m, s in ranked[:3]]}"
                )
                if best_sim >= AUTO_MERGE_THRESHOLD:
                    logger.info(
                        f"  Auto-merge (sim={best_sim:.2f}): '{name}' -> '{best_meta['name']}'")
                    canonical_map[name] = best_meta['name']
                elif best_sim >= LLM_CONFIRM_THRESHOLD:
                    top_candidates = [m for m, s in ranked[:3]
                                      if s >= LLM_CONFIRM_THRESHOLD]
                    sample_attr = unique_by_name[name]
                    llm_batch.append({
                        "raw_name": name,
                        "raw_value": sample_attr.get('value'),
                        "raw_unit": sample_attr.get('unit'),
                        "candidates": top_candidates,
                    })
                    logger.info(
                        f"[DBMatch] llm_confirm_batch_size={len(llm_batch)}")
                else:
                    still_unresolved.append(name)
            if llm_batch:
                llm_results = await _llm_confirm_batch(llm_batch, llm_provider)
                logger.info(f"[DBMatch] llm_results={llm_results}")
                for name, matched in llm_results.items():
                    if matched:
                        canonical_map[name] = matched
                    else:
                        still_unresolved.append(name)
            unresolved = still_unresolved
    if len(unresolved) >= 2:
        logger.info(
            f"Local clustering {len(unresolved)} attrs with no DB match")
        unresolved_embeddings = await asyncio.to_thread(model.encode, unresolved)
        clusters = []
        used = set()
        for i, name_i in enumerate(unresolved):
            if name_i in used:
                continue
            cluster = [name_i]
            used.add(name_i)
            for j, name_j in enumerate(unresolved):
                if j <= i or name_j in used:
                    continue
                if _should_skip_container_clustering(name_i, name_j):
                    continue
                similarity = 1 - \
                    cosine(unresolved_embeddings[i], unresolved_embeddings[j])
                if similarity >= threshold:
                    cluster.append(name_j)
                    used.add(name_j)
            clusters.append(cluster)
        for cluster in clusters:
            if len(cluster) == 1:
                canonical_map[cluster[0]] = cluster[0]
                continue
            if canonical_names:
                matched_existing = next(
                    (n for n in cluster if n in canonical_names), None)
                if matched_existing:
                    for alias in cluster:
                        canonical_map[alias] = matched_existing
                    continue
            longest = max(cluster, key=len)
            for alias in cluster:
                canonical_map[alias] = longest
    else:
        for name in unresolved:
            canonical_map[name] = name
    canonical_map = await _value_collision_check(raw_attrs, canonical_map, 'gemini')
    remapped = []
    for attr in raw_attrs:
        remapped_attr = attr.copy()
        remapped_attr['name'] = canonical_map.get(attr['name'], attr['name'])
        remapped.append(remapped_attr)
    return remapped
