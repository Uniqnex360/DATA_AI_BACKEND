from app.schemas.enrichment import RawValue, StandardizedAttribute
from .rules import BUSINESS_RULES, SOURCE_CONFIDENCE
from .utils import extract_number
from typing import List


# def standardize_attribute(attribute: str, values: List[RawValue]) -> StandardizedAttribute:
#     if not values:
#         raise ValueError("No values to standardize")

#     values = sorted(values, key=lambda v: SOURCE_CONFIDENCE.get(
#         v.source, 0.5), reverse=True)
#     chosen = values[0]
#     base_confidence = float(SOURCE_CONFIDENCE.get(chosen.source, 0.5))

#     if attribute in BUSINESS_RULES and 'allowed' in BUSINESS_RULES[attribute]:
#         for v in values:
#             value_text = v.value.lower()
#             for allowed in BUSINESS_RULES[attribute]['allowed']:
#                 if allowed.lower() in value_text:
#                     return StandardizedAttribute(
#                         standard_value=allowed,
#                         unit=None,
#                         derived_from=[k.value for k in values],
#                         confidence=base_confidence + 0.05,
#                         reason=f"Matched allowed enum: {allowed}"
#                     )

#     if attribute in BUSINESS_RULES and BUSINESS_RULES[attribute].get('type') == 'numeric':
#         num = extract_number(chosen.value)
#         if num is None:
#             raise ValueError(f"Cannot extract number from {chosen.value}")
#         unit = "inch" if any(x in chosen.value.lower()
#                              for x in ["inch", "”", "\""]) else None
#         return StandardizedAttribute(
#             standard_value=num,
#             unit=unit,
#             derived_from=[v.value for v in values],
#             confidence=base_confidence,
#             reason=f"Selected highest confidence source: {chosen.source}"
#         )

#     return StandardizedAttribute(
#         standard_value=chosen.value,
#         unit=None,
#         derived_from=[v.value for v in values],
#         confidence=base_confidence,
#         reason=f"Selected highest confidence source: {chosen.source}"
#     )
from typing import List, Optional, Dict, Any
import re
from dataclasses import dataclass

@dataclass
class RawValue:
    value: Any
    source: str

@dataclass
class StandardizedAttribute:
    standard_value: Any
    unit: Optional[str]
    derived_from: List[Any]
    confidence: float
    reason: str
    conflicts: Optional[List[Dict]] = None

# Confidence by source type
SOURCE_CONFIDENCE = {
    'manufacturer': 0.95,
    'official_site': 0.90,
    'datasheet': 0.90,
    'web': 0.70,
    'vendor': 0.65,
    'marketplace': 0.60,
    'user_manual': 0.85
}

# Business rules for attributes
BUSINESS_RULES = {
    'color': {
        'type': 'enum',
        'allowed': ['Black', 'White', 'Red', 'Blue', 'Silver', 'Gold'],
        'multi_value': True  # Can have multiple colors
    },
    'display_size': {
        'type': 'numeric',
        'unit': 'inches',
        'min': 0.5,
        'max': 100
    },
    'battery_capacity': {
        'type': 'numeric',
        'unit': 'mAh',
        'min': 100,
        'max': 100000
    },
    'weight': {
        'type': 'numeric',
        'unit': ['g', 'kg', 'lb', 'oz'],
        'min': 1,
        'max': 10000
    }
}

def clean_value(value: str) -> str:
    """Clean and normalize a value"""
    if not isinstance(value, str):
        return str(value)
    
    # Remove extra whitespace
    value = ' '.join(value.split())
    
    # Decode HTML entities
    value = value.replace('&quot;', '"').replace('&apos;', "'")
    value = value.replace('&#34;', '"').replace('&#39;', "'")
    
    # Normalize quotes
    value = value.replace('"', '"').replace('"', '"')
    value = value.replace(''', "'").replace(''', "'")
    
    return value.strip()

def extract_number_and_unit(value: str) -> tuple[Optional[float], Optional[str]]:
    """Extract number and unit from string like '308 mAh' or '1.9"' """
    if not isinstance(value, str):
        return None, None
    
    # Pattern: number (possibly with decimal) followed by optional unit
    patterns = [
        r'([\d,]+\.?\d*)\s*([a-zA-Z]+)',  # "308 mAh" or "1.9 inches"
        r'([\d,]+\.?\d*)\s*([""′″])',      # "1.9"" or "45'"
        r'([\d,]+\.?\d*)$',                 # Just number "308"
    ]
    
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            num_str = match.group(1).replace(',', '')
            try:
                num = float(num_str)
                unit = match.group(2) if len(match.groups()) > 1 else None
                
                # Normalize units
                if unit:
                    unit = unit.strip().lower()
                    # Convert " to inches
                    if unit in ['"', '″', 'inch']:
                        unit = 'inches'
                    elif unit == "'":
                        unit = 'feet'
                
                return num, unit
            except ValueError:
                continue
    
    return None, None

def values_are_similar(v1: str, v2: str, threshold: float = 0.9) -> bool:
    """Check if two values are similar (fuzzy match)"""
    from difflib import SequenceMatcher
    
    v1_clean = clean_value(str(v1)).lower()
    v2_clean = clean_value(str(v2)).lower()
    
    if v1_clean == v2_clean:
        return True
    
    # Fuzzy match
    similarity = SequenceMatcher(None, v1_clean, v2_clean).ratio()
    return similarity >= threshold

def deduplicate_values(values: List[RawValue]) -> List[RawValue]:
    """Remove duplicate/similar values, keeping highest confidence"""
    if not values:
        return []
    
    unique = []
    seen_values = []
    
    for val in values:
        is_duplicate = False
        for seen in seen_values:
            if values_are_similar(val.value, seen):
                is_duplicate = True
                break
        
        if not is_duplicate:
            unique.append(val)
            seen_values.append(val.value)
    
    return unique

def resolve_conflicts(values: List[RawValue], attr_name: str) -> Dict[str, Any]:
    """Resolve conflicting values intelligently"""
    
    if not values:
        return {'chosen': None, 'conflicts': [], 'reason': 'No values'}
    
    # Check if values are numeric
    numeric_values = []
    for val in values:
        num, unit = extract_number_and_unit(str(val.value))
        if num is not None:
            numeric_values.append({
                'value': val.value,
                'number': num,
                'unit': unit,
                'source': val.source,
                'confidence': SOURCE_CONFIDENCE.get(val.source, 0.5)
            })
    
    if numeric_values:
        # For numeric conflicts, use multiple strategies
        
        # Strategy 1: Pick highest confidence if values are very different
        sorted_by_conf = sorted(numeric_values, key=lambda x: x['confidence'], reverse=True)
        highest_conf = sorted_by_conf[0]
        
        # Strategy 2: Check if values cluster (most sources agree)
        numbers = [v['number'] for v in numeric_values]
        avg = sum(numbers) / len(numbers)
        
        # Find most common value (within 5% tolerance)
        clusters = {}
        for nv in numeric_values:
            found_cluster = False
            for cluster_key in clusters:
                if abs(nv['number'] - cluster_key) / cluster_key <= 0.05:
                    clusters[cluster_key].append(nv)
                    found_cluster = True
                    break
            
            if not found_cluster:
                clusters[nv['number']] = [nv]
        
        # Pick the cluster with highest total confidence
        best_cluster = max(clusters.items(), key=lambda x: sum(v['confidence'] for v in x[1]))
        consensus_value = best_cluster[1][0]  # Pick first in best cluster
        
        # If consensus differs from highest confidence, flag conflict
        if abs(consensus_value['number'] - highest_conf['number']) / highest_conf['number'] > 0.1:
            return {
                'chosen': consensus_value['value'],
                'conflicts': [v['value'] for v in numeric_values],
                'reason': f"Consensus from {len(best_cluster[1])} sources vs highest confidence"
            }
        else:
            return {
                'chosen': highest_conf['value'],
                'conflicts': [v['value'] for v in numeric_values if v != highest_conf],
                'reason': f"Highest confidence source: {highest_conf['source']}"
            }
    
    # For non-numeric, just pick highest confidence
    sorted_vals = sorted(values, key=lambda v: SOURCE_CONFIDENCE.get(v.source, 0.5), reverse=True)
    return {
        'chosen': sorted_vals[0].value,
        'conflicts': [v.value for v in sorted_vals[1:]],
        'reason': f"Highest confidence source: {sorted_vals[0].source}"
    }

def standardize_attribute(attribute: str, values: List[RawValue]) -> StandardizedAttribute:
    """
    Improved standardization with:
    - Data cleaning
    - Deduplication
    - Conflict resolution
    - Better unit extraction
    - Multi-value support
    """
    
    if not values:
        raise ValueError("No values to standardize")
    
    # 1. CLEAN all values
    cleaned_values = []
    for v in values:
        cleaned_val = clean_value(str(v.value))
        if cleaned_val:  # Only keep non-empty
            cleaned_values.append(RawValue(value=cleaned_val, source=v.source))
    
    if not cleaned_values:
        raise ValueError("All values were empty after cleaning")
    
    # 2. DEDUPLICATE similar values
    unique_values = deduplicate_values(cleaned_values)
    
    # 3. SORT by source confidence
    unique_values = sorted(
        unique_values,
        key=lambda v: SOURCE_CONFIDENCE.get(v.source, 0.5),
        reverse=True
    )
    
    # 4. CHECK if business rule exists
    rule = BUSINESS_RULES.get(attribute, {})
    
    # 5. HANDLE MULTI-VALUE attributes (like colors, features)
    if rule.get('multi_value'):
        # Collect all unique values
        all_values = list(set(v.value for v in unique_values))
        avg_confidence = sum(SOURCE_CONFIDENCE.get(v.source, 0.5) for v in unique_values) / len(unique_values)
        
        return StandardizedAttribute(
            standard_value=all_values,
            unit=None,
            derived_from=[v.value for v in values],
            confidence=avg_confidence,
            reason=f"Collected {len(all_values)} unique values"
        )
    
    # 6. HANDLE ENUM attributes (with allowed values)
    if rule.get('type') == 'enum' and 'allowed' in rule:
        for v in unique_values:
            value_text = v.value.lower()
            for allowed in rule['allowed']:
                if allowed.lower() in value_text:
                    base_conf = SOURCE_CONFIDENCE.get(v.source, 0.5)
                    return StandardizedAttribute(
                        standard_value=allowed,
                        unit=None,
                        derived_from=[k.value for k in values],
                        confidence=base_conf + 0.05,
                        reason=f"Matched allowed enum: {allowed}",
                        conflicts=[v.value for v in unique_values if v != unique_values[0]]
                    )
    
    # 7. HANDLE NUMERIC attributes
    if rule.get('type') == 'numeric':
        # Resolve conflicts for numeric values
        resolution = resolve_conflicts(unique_values, attribute)
        
        num, unit = extract_number_and_unit(str(resolution['chosen']))
        
        if num is None:
            raise ValueError(f"Cannot extract number from {resolution['chosen']}")
        
        # Validate against min/max if specified
        if 'min' in rule and num < rule['min']:
            raise ValueError(f"Value {num} below minimum {rule['min']}")
        if 'max' in rule and num > rule['max']:
            raise ValueError(f"Value {num} above maximum {rule['max']}")
        
        # Use rule's expected unit if not found
        if not unit and 'unit' in rule:
            if isinstance(rule['unit'], list):
                unit = rule['unit'][0]  # Default to first unit
            else:
                unit = rule['unit']
        
        return StandardizedAttribute(
            standard_value=num,
            unit=unit,
            derived_from=[v.value for v in values],
            confidence=SOURCE_CONFIDENCE.get(unique_values[0].source, 0.5),
            reason=resolution['reason'],
            conflicts=resolution.get('conflicts')
        )
    
    # 8. DEFAULT: Pick highest confidence value
    resolution = resolve_conflicts(unique_values, attribute)
    
    return StandardizedAttribute(
        standard_value=resolution['chosen'],
        unit=None,
        derived_from=[v.value for v in values],
        confidence=SOURCE_CONFIDENCE.get(unique_values[0].source, 0.5),
        reason=resolution['reason'],
        conflicts=resolution.get('conflicts')
    )
