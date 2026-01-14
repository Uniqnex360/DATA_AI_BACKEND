from app.schemas.enrichment import RawValue, StandardizedAttribute
from .rules import BUSINESS_RULES, SOURCE_CONFIDENCE
from .utils import extract_number
from typing import List
def standardize_attribute(attribute: str, values: List[RawValue]) -> StandardizedAttribute:
    if not values:
        raise ValueError("No values to standardize")

    values = sorted(values, key=lambda v: SOURCE_CONFIDENCE.get(v.source, 0.5), reverse=True)
    chosen = values[0]
    base_confidence = float(SOURCE_CONFIDENCE.get(chosen.source, 0.5))  

    
    if attribute in BUSINESS_RULES and 'allowed' in BUSINESS_RULES[attribute]:
        for v in values:
            value_text = v.value.lower()
            for allowed in BUSINESS_RULES[attribute]['allowed']:
                if allowed.lower() in value_text:
                    return StandardizedAttribute(
                        standard_value=allowed,
                        unit=None,
                        derived_from=[k.value for k in values],
                        confidence=base_confidence + 0.05,   
                        reason=f"Matched allowed enum: {allowed}"
                    )

    
    if attribute in BUSINESS_RULES and BUSINESS_RULES[attribute].get('type') == 'numeric':
        num = extract_number(chosen.value)
        if num is None:
            raise ValueError(f"Cannot extract number from {chosen.value}")
        unit = "inch" if any(x in chosen.value.lower() for x in ["inch", "”", "\""]) else None
        return StandardizedAttribute(
            standard_value=num,
            unit=unit,
            derived_from=[v.value for v in values],
            confidence=base_confidence,
            reason=f"Selected highest confidence source: {chosen.source}"
        )

    
    return StandardizedAttribute(
        standard_value=chosen.value,
        unit=None,
        derived_from=[v.value for v in values],
        confidence=base_confidence,
        reason=f"Selected highest confidence source: {chosen.source}"
    )