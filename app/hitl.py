from app.schemas.enrichment import ReviewItem
from .hitl_store import HITL_QUEUE
from app.core.config import settings

def check_for_human_review(product_key:str,attribute:str,standarized_attr):
    if(standarized_attr.confidence<settings.hitl_confidence_threashold):
        item=ReviewItem(product_key=product_key,attribute=attribute,proposed_value=standarized_attr.standard_value,confidence=standarized_attr.confidence,reason=standarized_attr.reason,derived_from=standarized_attr.derived_from)
        HITL_QUEUE.setdefault(product_key,[]).append(item.model_dump())
        return True
    return False