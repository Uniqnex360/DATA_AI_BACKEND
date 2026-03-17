import logging
from typing import List, Dict, Any, Optional
from collections import Counter
from app.opensource_aggregation.models.schemas import (
    UnifiedAttribute, GoldenRecord, ProductIdentifier, ExtractedAttribute
)
from app.opensource_aggregation.config import config
logger = logging.getLogger("os_conflict_resolver")
class ConflictResolver:
    def resolve(
        self,
        product: ProductIdentifier,
        unified_attributes: List[UnifiedAttribute],
        sources_consulted: List[str],
        image_url: Optional[str] = None
    ) -> GoldenRecord:
        final_attributes: Dict[str, Any] = {}
        conflicts: Dict[str, List[str]] = {}
        for unified in unified_attributes:
            if not unified.values:
                continue
            best_value, has_conflict = self._pick_best_value(unified)
            if best_value:
                final_attributes[unified.canonical_name] = best_value
            if has_conflict:
                conflicts[unified.canonical_name] = [
                    f"{v.value} (from {v.source_url})"
                    for v in unified.values
                ]
        confidence = self._calculate_overall_confidence(unified_attributes)
        golden = GoldenRecord(
            brand=product.brand,
            mpn=product.mpn,
            title=product.title,
            attributes=final_attributes,
            image_url=image_url,
            sources_consulted=sources_consulted,
            confidence_score=confidence,
            conflicts=conflicts
        )
        logger.info(
            f" Golden record: {len(final_attributes)} attributes, "
            f"confidence: {confidence:.2f}, "
            f"conflicts: {len(conflicts)}"
        )
        return golden
    def _pick_best_value(
        self, unified: UnifiedAttribute
    ) -> tuple[Optional[str], bool]:
        values = unified.values
        if not values:
            return None, False
        normalized = {}
        for v in values:
            norm = self._normalize_value(v.value)
            if norm not in normalized:
                normalized[norm] = []
            normalized[norm].append(v)
        if len(normalized) == 1:
            best = values[0]
            return best.value, False
        has_conflict = True
        value_scores: Dict[str, float] = {}
        for norm_value, attrs in normalized.items():
            confidence_sum = sum(a.confidence for a in attrs)
            frequency_bonus = len(attrs) * 0.1  
            total_score = confidence_sum + frequency_bonus
            value_scores[norm_value] = total_score
        best_norm = max(value_scores, key=value_scores.get)
        best_attr = normalized[best_norm][0]
        return best_attr.value, has_conflict
    def _normalize_value(self, value: str) -> str:
        import re
        v = value.lower().strip()
        v = v.replace('"', ' inch')
        v = v.replace("'", ' feet')
        v = v.replace('inches', 'inch')
        v = v.replace('lbs', 'lb')
        v = v.replace('pounds', 'lb')
        v = v.replace('kilograms', 'kg')
        v = re.sub(r'\s+', ' ', v)
        return v
    def _calculate_overall_confidence(
        self, unified_attributes: List[UnifiedAttribute]
    ) -> float:
        if not unified_attributes:
            return 0.0
        scores = []
        for unified in unified_attributes:
            if unified.values:
                avg_confidence = sum(
                    v.confidence for v in unified.values
                ) / len(unified.values)
                scores.append(avg_confidence)
        if not scores:
            return 0.0
        return sum(scores) / len(scores)