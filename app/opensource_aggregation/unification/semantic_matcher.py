import logging
import numpy as np
from typing import List, Dict, Tuple
from collections import defaultdict
from app.opensource_aggregation.models.schemas import (
    ExtractedAttribute, UnifiedAttribute
)
from app.opensource_aggregation.config import config
logger = logging.getLogger("os_semantic_matcher")
class SemanticMatcher:
    def __init__(self):
        self._model = None
        self._manual_mappings = self._build_manual_mappings()
    @property
    def model(self):
        if self._model is None:
            logger.info(f"Loading embedding model: {config.embedding_model}")
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(config.embedding_model)
            logger.info(" Embedding model loaded")
        return self._model
    def _build_manual_mappings(self) -> Dict[str, str]:
        return {
            "display size": "screen_size",
            "screen size": "screen_size",
            "diagonal": "screen_size",
            "monitor size": "screen_size",
            "weight": "weight",
            "net weight": "weight",
            "product weight": "weight",
            "shipping weight": "shipping_weight",
            "material": "material",
            "construction": "material",
            "body material": "material",
            "housing material": "material",
            "color": "color",
            "colour": "color",
            "finish": "color",
            "operating temperature": "operating_temperature",
            "temperature range": "operating_temperature",
            "working temperature": "operating_temperature",
            "dimensions": "dimensions",
            "size": "dimensions",
            "product dimensions": "dimensions",
            "overall dimensions": "dimensions",
            "length x width x height": "dimensions",
        }
    def unify_attributes(
        self, all_attributes: List[ExtractedAttribute]
    ) -> List[UnifiedAttribute]:
        if not all_attributes:
            return []
        attr_groups: Dict[str, List[ExtractedAttribute]] = defaultdict(list)
        unmapped: List[ExtractedAttribute] = []
        for attr in all_attributes:
            name_lower = attr.name.lower().strip()
            if name_lower in self._manual_mappings:
                canonical = self._manual_mappings[name_lower]
                attr_groups[canonical].append(attr)
            else:
                unmapped.append(attr)
        if unmapped:
            groups = self._group_by_embedding(unmapped)
            for canonical_name, attrs in groups.items():
                attr_groups[canonical_name].extend(attrs)
        result = []
        for canonical_name, attrs in attr_groups.items():
            result.append(UnifiedAttribute(
                canonical_name=canonical_name,
                values=attrs
            ))
        logger.info(
            f" Unified {len(all_attributes)} attributes into "
            f"{len(result)} canonical attributes"
        )
        return result
    def _group_by_embedding(
        self, attributes: List[ExtractedAttribute]
    ) -> Dict[str, List[ExtractedAttribute]]:
        from sklearn.metrics.pairwise import cosine_similarity
        if not attributes:
            return {}
        unique_names = list(set(attr.name for attr in attributes))
        if len(unique_names) == 1:
            canonical = self._to_canonical_name(unique_names[0])
            return {canonical: attributes}
        embeddings = self.model.encode(unique_names)
        sim_matrix = cosine_similarity(embeddings)
        groups: Dict[str, List[str]] = {}
        assigned = set()
        for i in range(len(unique_names)):
            if unique_names[i] in assigned:
                continue
            group_names = [unique_names[i]]
            assigned.add(unique_names[i])
            for j in range(i + 1, len(unique_names)):
                if unique_names[j] in assigned:
                    continue
                if sim_matrix[i][j] >= config.similarity_threshold:
                    group_names.append(unique_names[j])
                    assigned.add(unique_names[j])
            canonical = self._to_canonical_name(
                min(group_names, key=len)
            )
            groups[canonical] = group_names
        result: Dict[str, List[ExtractedAttribute]] = defaultdict(list)
        name_to_canonical = {}
        for canonical, names in groups.items():
            for name in names:
                name_to_canonical[name] = canonical
        for attr in attributes:
            canonical = name_to_canonical.get(
                attr.name,
                self._to_canonical_name(attr.name)
            )
            result[canonical].append(attr)
        return dict(result)
    def _to_canonical_name(self, name: str) -> str:
        import re
        canonical = name.lower().strip()
        canonical = re.sub(r'[^a-z0-9]+', '_', canonical)
        canonical = canonical.strip('_')
        return canonical