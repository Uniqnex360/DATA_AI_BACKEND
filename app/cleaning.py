from typing import List
from app.schemas.enrichment import CleaningResult, RawValue
from app.utils.validators import is_invalid, normalize_text
def clean_attribute(values:List[RawValue])->CleaningResult:
    valid=[]
    removed=[]
    seen=set()
    if not values:
        return CleaningResult(valid_results=[],removed_values=[])
    for item in values:
        raw=normalize_text(item.value)
        if(is_invalid(raw)):
            removed.append({'value':item.value,'reason':'invalid'})
            continue
        key=raw.lower()
        if(key in seen):
            removed.append({'value':item.value,'reason':'duplicate'})
            continue
        seen.add(key)
        valid.append(RawValue(value=raw,source=item.source))
    return CleaningResult(valid_values=valid,removed_values=removed)
            
            