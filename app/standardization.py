import re
import logging
from typing import List, Optional, Dict, Any, Union
from difflib import SequenceMatcher
from app.schemas.enrichment import RawValue, StandardizedAttribute

logger = logging.getLogger("standardization_engine")


SOURCE_CONFIDENCE = {
    'manufacturer': 0.95,
    'official_site': 0.90,
    'truth_engine': 0.88,
    'datasheet': 0.90,
    'web': 0.70,
    'vendor': 0.65,
    'inference': 0.40 
}

def clean_value(value: str) -> str:
    if not isinstance(value, str):
        return str(value)
    value = ' '.join(value.split())
    value = value.replace('&quot;', '"').replace('&apos;', "'").replace('&#34;', '"').replace('&#39;', "'")
    value = value.replace('"', '"').replace('"', '"').replace(''', "'").replace(''', "'")
    return value.strip()

def extract_number_and_unit(value: str) -> tuple[Optional[float], Optional[str]]:
    if not isinstance(value, str):
        return None, None
    
    patterns = [
        r'([\d,]+\.?\d*)\s*([a-zA-Z]+)', 
        r'([\d,]+\.?\d*)\s*([""′″])',      
        r'([\d,]+\.?\d*)$',                
    ]
    
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            num_str = match.group(1).replace(',', '')
            try:
                num = float(num_str)
                unit = match.group(2) if len(match.groups()) > 1 else None
                if unit:
                    unit = unit.strip().lower()
                    if unit in ['"', '″', 'inch']: unit = 'inches'
                    elif unit == "'": unit = 'feet'
                return num, unit
            except ValueError:
                continue
    return None, None

def values_are_similar(v1: str, v2: str, threshold: float = 0.9) -> bool:
    v1_clean = clean_value(str(v1)).lower()
    v2_clean = clean_value(str(v2)).lower()
    if v1_clean == v2_clean: return True
    return SequenceMatcher(None, v1_clean, v2_clean).ratio() >= threshold

def deduplicate_values(values: List[RawValue]) -> List[RawValue]:
    if not values: return []
    unique, seen_values = [], []
    for val in values:
        if not any(values_are_similar(val.value, seen) for seen in seen_values):
            unique.append(val)
            seen_values.append(val.value)
    return unique

def resolve_conflicts(values: List[RawValue], attr_name: str) -> Dict[str, Any]:
    if not values:
        return {'chosen': None, 'conflicts': [], 'reason': 'No values'}
    
    numeric_values = []
    for val in values:
        num, unit = extract_number_and_unit(str(val.value))
        if num is not None:
            numeric_values.append({
                'value': val.value, 'number': num, 'unit': unit, 
                'source': val.source, 'confidence': SOURCE_CONFIDENCE.get(val.source, 0.5)
            })
    
    if numeric_values:
        sorted_by_conf = sorted(numeric_values, key=lambda x: x['confidence'], reverse=True)
        highest_conf = sorted_by_conf[0]
        
        clusters = {}
        for nv in numeric_values:
            found_cluster = False
            for cluster_key in clusters:
                if abs(nv['number'] - cluster_key) / (cluster_key or 1) <= 0.05:
                    clusters[cluster_key].append(nv)
                    found_cluster = True
                    break
            if not found_cluster: clusters[nv['number']] = [nv]
        
        best_cluster = max(clusters.items(), key=lambda x: sum(v['confidence'] for v in x[1]))
        consensus_value = best_cluster[1][0]
        
        if abs(consensus_value['number'] - highest_conf['number']) / (highest_conf['number'] or 1) > 0.1:
            return {
                'chosen': consensus_value['value'],
                'conflicts': [v['value'] for v in numeric_values if v['value'] != consensus_value['value']],
                'reason': f"Consensus from {len(best_cluster[1])} sources"
            }
        return {
            'chosen': highest_conf['value'],
            'conflicts': [v['value'] for v in numeric_values if v['value'] != highest_conf['value']],
            'reason': f"Highest confidence source: {highest_conf['source']}"
        }
    
    sorted_vals = sorted(values, key=lambda v: SOURCE_CONFIDENCE.get(v.source, 0.5), reverse=True)
    return {
        'chosen': sorted_vals[0].value,
        'conflicts': [v.value for v in sorted_vals[1:]],
        'reason': f"Highest confidence source: {sorted_vals[0].source}"
    }

def standardize_attribute(attribute: str, values: List[RawValue], rules: dict) -> StandardizedAttribute:
 
    if not values:
        raise ValueError("No values to standardize")
    
    cleaned_values = [RawValue(value=clean_value(str(v.value)), source=v.source) 
                      for v in values if clean_value(str(v.value))]
    
    if not cleaned_values:
        raise ValueError("All values were empty after cleaning")
    
    unique_values = deduplicate_values(cleaned_values)
    unique_values = sorted(unique_values, key=lambda v: SOURCE_CONFIDENCE.get(v.source, 0.5), reverse=True)
    
    rule = rules.get(attribute, {})
    
    if rule.get('multi_value'):
        all_vals = list(set(str(v.value) for v in unique_values))
        total_conf = sum(SOURCE_CONFIDENCE.get(v.source, 0.5) for v in unique_values)
        avg_conf = total_conf / len(unique_values) if unique_values else 0.5
        
        return StandardizedAttribute(
            standard_value=", ".join(all_vals), 
            unit=None,
            derived_from=[v.source for v in unique_values],
            confidence=round(avg_conf, 2),
            reason=f"Aggregated {len(all_vals)} unique values"
        )
    
    if rule.get('type') == 'enum' and 'allowed' in rule:
        for v in unique_values:
            val_lower = str(v.value).lower()
            for allowed in rule['allowed']:
                if allowed.lower() in val_lower:
                    return StandardizedAttribute(
                        standard_value=allowed,
                        unit=None,
                        derived_from=[x.source for x in unique_values],
                        confidence=min(SOURCE_CONFIDENCE.get(v.source, 0.5) + 0.05, 1.0),
                        reason=f"Matched allowed enum: {allowed}",
                        conflicts=[x.value for x in unique_values if x.value != allowed]
                    )
    
    if rule.get('type') == 'numeric':
        res = resolve_conflicts(unique_values, attribute)
        num, unit = extract_number_and_unit(str(res['chosen']))
        
        if num is None:
            return StandardizedAttribute(
                standard_value=str(res['chosen']),
                unit=None,
                derived_from=[v.source for v in unique_values],
                confidence=0.5,
                reason="Failed to parse numeric value, stored as string"
            )
        
        if 'min' in rule and num < rule['min']: num = rule['min']
        if 'max' in rule and num > rule['max']: num = rule['max']
        
        return StandardizedAttribute(
            standard_value=num,
            unit=unit or rule.get('unit'),
            derived_from=[v.source for v in unique_values],
            confidence=SOURCE_CONFIDENCE.get(unique_values[0].source, 0.5),
            reason=res['reason'],
            conflicts=res.get('conflicts')
        )
    
    res = resolve_conflicts(unique_values, attribute)
    return StandardizedAttribute(
        standard_value=res['chosen'],
        unit=None,
        derived_from=[v.source for v in unique_values],
        confidence=SOURCE_CONFIDENCE.get(unique_values[0].source, 0.5),
        reason=res['reason'],
        conflicts=res.get('conflicts')
    )