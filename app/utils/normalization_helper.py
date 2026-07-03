from typing import Dict, List
import re

def normalize_concatenated_uom(attrs: List[Dict]) -> List[Dict]:
    uom_pattern = r'^([\d\.]+)\s*([a-zA-Z]+)$'
    for attr in attrs:
        if not attr.get('unit') and isinstance(attr.get('value'), str):
            match = re.match(uom_pattern, attr['value'].strip())
            if match:
                val, uom = match.groups()
                attr['value'] = val
                attr['unit'] = uom.lower() 
    return attrs