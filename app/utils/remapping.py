import asyncio
import logging
import re
from typing import Dict, List
from sentence_transformers import SentenceTransformer
import numpy as np
from scipy.spatial.distance import cosine

logger = logging.getLogger("cluster_attributes_by_meaning")
_embedding_model = None

_CONTAINER_TERMS = {
    'carton', 'pack', 'pallet', 'box', 'case', 'each', 'unit', 'piece', 'bag', 'sleeve'
}


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
    from app.main import _global_embedding_model
    if _global_embedding_model is None:
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer('all-MiniLM-L6-v2', device='cpu')
    return _global_embedding_model

async def cluster_attributes_by_meaning(
    raw_attrs: List[Dict],
    canonical_names: List[str] = None,
    threshold: float = 0.85
) -> List[Dict]:
    if not raw_attrs:
        return raw_attrs

    model = await get_embedding_model()
    attr_names = list(set(attr['name'] for attr in raw_attrs))

    if len(attr_names) < 2:
        return raw_attrs

    logger.info(f"Clustering {len(attr_names)} unique attribute names")

    embeddings = await asyncio.to_thread(model.encode, attr_names)

    clusters = []
    used = set()

    for i, name_i in enumerate(attr_names):
        if name_i in used:
            continue
        cluster = [name_i]
        used.add(name_i)

        for j, name_j in enumerate(attr_names):
            if j <= i or name_j in used:
                continue
            if _should_skip_container_clustering(name_i, name_j):
                logger.debug(
                    f"  Skipping cluster between distinct container terms: '{name_i}' and '{name_j}'"
                )
                continue
            similarity = 1 - cosine(embeddings[i], embeddings[j])
            if similarity >= threshold:
                cluster.append(name_j)
                used.add(name_j)
                logger.debug(f"  {name_i} ≈ {name_j} (sim={similarity:.2f})")

        clusters.append(cluster)

    logger.info(f"Found {len(clusters)} semantic clusters")

    canonical_map = {}
    for cluster in clusters:
        if len(cluster) == 1:
            canonical_map[cluster[0]] = cluster[0]
            continue

        if canonical_names:
            for name in cluster:
                if name in canonical_names:
                    canonical_map[name] = name
                    for alias in cluster:
                        if alias != name:
                            canonical_map[alias] = name
                    break

        if cluster[0] not in canonical_map:
            longest = max(cluster, key=len)
            canonical_map[longest] = longest
            for alias in cluster:
                if alias != longest:
                    canonical_map[alias] = longest

        logger.info(f"  Cluster canonical: {canonical_map[cluster[0]]}")

    remapped = []
    for attr in raw_attrs:
        remapped_attr = attr.copy()
        remapped_attr['name'] = canonical_map.get(attr['name'], attr['name'])
        remapped.append(remapped_attr)

    return remapped