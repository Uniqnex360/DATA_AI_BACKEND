import logging
from typing import Dict, List

from sentence_transformers import SentenceTransformer
import numpy as np
from scipy.spatial.distance import cosine
import logging

logger = logging.getLogger("aggregate_product")

# Load model ONCE at module level
_embedding_model = None

async def get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        logger.info("Loading SentenceTransformer model...")
        _embedding_model = SentenceTransformer('all-MiniLM-L6-v2')
        logger.info("✓ Model loaded")
    return _embedding_model
logger=logging.getLogger("cluster_remap")
async def cluster_attributes_by_meaning(
    raw_attrs: List[Dict],
    canonical_names: List[str] = None,
    threshold: float = 0.85
) -> List[Dict]:
    """
    Cluster raw attributes by semantic similarity.
    Group synonyms, pick canonical name per cluster.
    Returns remapped raw_attrs with unified names.
    """
    if not raw_attrs:
        return raw_attrs
    
    # Load embedding model (lightweight, fast)
    model = await get_embedding_model() 
    
    # Extract unique attr names
    attr_names = list(set(attr['name'] for attr in raw_attrs))
    if len(attr_names) < 2:
        return raw_attrs  # Nothing to cluster
    
    logger.info(f"Clustering {len(attr_names)} unique attribute names")
    
    # Embed all names
    embeddings = model.encode(attr_names)
    
    # Hierarchical clustering by cosine similarity
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
            
            similarity = 1 - cosine(embeddings[i], embeddings[j])
            
            if similarity >= threshold:
                cluster.append(name_j)
                used.add(name_j)
                logger.debug(f"  {name_i} ≈ {name_j} (sim={similarity:.2f})")
        
        clusters.append(cluster)
    
    logger.info(f"Found {len(clusters)} semantic clusters")
    
    # Pick canonical name per cluster
    canonical_map = {}
    for cluster in clusters:
        if len(cluster) == 1:
            canonical_map[cluster[0]] = cluster[0]
            continue
        
        # Priority: 1) in canonical_names DB list, 2) longest name, 3) first
        if canonical_names:
            for name in cluster:
                if name in canonical_names:
                    canonical_map[name] = name
                    for alias in cluster:
                        if alias != name:
                            canonical_map[alias] = name
                    break
        
        if cluster[0] not in canonical_map:  # No DB match found
            longest = max(cluster, key=len)
            canonical_map[longest] = longest
            for alias in cluster:
                if alias != longest:
                    canonical_map[alias] = longest
        
        logger.info(f"  Cluster canonical: {canonical_map[cluster[0]]}")
    
    # Remap raw attrs
    remapped = []
    for attr in raw_attrs:
        remapped_attr = attr.copy()
        remapped_attr['name'] = canonical_map.get(attr['name'], attr['name'])
        remapped.append(remapped_attr)
    
    return remapped