# app/utils/attribute_dedupe.py

import re
from typing import Dict, Any

def normalize_attr_name(name: str) -> str:
    if not name:
        return ""
    return re.sub(r'[^a-z0-9]', '', name.lower().strip())

def deduplicate_product_attributes(attributes: Dict[str, Any]) -> Dict[str, Any]:
    
    if not attributes:
        return attributes
    
    deduped = {}
    normalized_map = {}
    
    for key, value in attributes.items():
        norm_key = normalize_attr_name(key)
        
        if norm_key not in normalized_map:
            normalized_map[norm_key] = key
            deduped[key] = value
        else:
            # Duplicate found - decide which to keep
            existing_key = normalized_map[norm_key]
            existing_val = deduped[existing_key]
            
            if isinstance(value, dict) and isinstance(existing_val, dict):
                new_conf = value.get('confidence', 0) or 0
                existing_conf = existing_val.get('confidence', 0) or 0
                
                if new_conf > existing_conf:
                    # Replace with new version
                    del deduped[existing_key]
                    deduped[key] = value
                    normalized_map[norm_key] = key
                elif new_conf == existing_conf and new_conf > 0:
                    # Merge sources
                    new_sources = value.get('sources', [])
                    existing_sources = existing_val.get('sources', [])
                    deduped[existing_key]['sources'] = list(set(existing_sources + new_sources))
    
    return deduped