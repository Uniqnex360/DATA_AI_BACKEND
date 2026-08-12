
import re
from typing import Optional


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

    return False