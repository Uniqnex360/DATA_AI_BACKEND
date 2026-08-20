
import re
from typing import List, Optional
import logging
logger=logging.getLogger(__name__)
BLOCKED_DISTRIBUTOR_ATTRIBUTES = {
    "itemnumber",
    "distributoritemnumber",
    "retaileritemnumber",
    "selleritemnumber",
    "storeitemnumber",

    "returnfee",
    "returnfees",
    "returnsfee",
    "returnsfees",
    "returnmethod",
    "returnsmethod",
    "returnpolicy",
    "returnspolicy",
    "refundpolicy",
    "refundmethod",
    "restockingfee",
    "restockingfees",

    "rating",
    "customerrating",
    "averagerating",
    "starrating",
    "reviewrating",
    "sellerrating",
    "storerating",

    "instock",
    "outofstock",
    "stock",
    "stockstatus",
    "stocklevel",
    "stockavailability",
    "availability",
    "inventorystatus",
    "inventorylevel",
    "unitsavailable",
    "quantityavailable",
    "stockquantity",
}


def normalize_attribute_label(name: Optional[str]) -> str:
    if not name:
        return ""

    text = str(name).strip().casefold()

    text = text.replace("#", " number ")
    text = re.sub(r"\bno\.?\b", " number ", text)

    return re.sub(r"[^a-z0-9]+", "", text)


def is_distributor_metadata(name: Optional[str]) -> bool:
    normalized = normalize_attribute_label(name)

    if not normalized:
        return False

    if normalized in BLOCKED_DISTRIBUTOR_ATTRIBUTES:
        return True
    if "price" in normalized and normalized not in {"price", "baseprice", "saleprice", "listprice"}:
        return True
    if normalized.startswith(("return", "returns", "refund")):
        if normalized.endswith(("fee", "fees", "method", "policy")):
            return True

    if normalized.endswith("rating"):
        rating_prefixes = (
            "customer",
            "average",
            "review",
            "star",
            "seller",
            "store",
        )
        if normalized.startswith(rating_prefixes):
            return True
        if "stock" in normalized or "availability" in normalized or "inventory" in normalized:
            return True

    return False

def filter_commerce_features(features: List[str]) -> List[str]:
    if not features:
        return features
        
    filtered = []
    commerce_keywords = [
        'shipping', 'free shipping', 'delivery', 'ship free', 'same day', '$50 ship', '$45',
        'financing', 'payment', 'revolving', 'installment', '29.99%', 'standard revolving',
        'in stock', 'available', 'stock status', 'inventory',
        'return', 'exchange', 'refund', 'hassle free', '90-day',
        'customer service', 'support', 'chat', 'phone', 'experts', 'live chat',
        'guarantee', 'pledge', 'satisfaction', 'shop with confidence', 'right part pledge'
    ]
    
    product_keywords = [
        'part', 'replaces', 'compatible', 'fits', 'diameter', 'width', 'material', 
        'design', 'construction', 'steel', 'aluminum', 'assembly', 'wheel', 'tire'
    ]
    
    for feature in features:
        feature_lower = feature.lower()
        
        if any(keyword in feature_lower for keyword in commerce_keywords):
            logger.info(f"[FEATURE FILTER] Removed commerce feature: {feature}")
            continue
            
        if (any(keyword in feature_lower for keyword in product_keywords) or 
            len(feature.split()) <= 8):
            filtered.append(feature)
        else:
            logger.info(f"[FEATURE FILTER] Removed non-product feature: {feature}")
    
    return filtered
