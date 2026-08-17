import re
import logging
from typing import List, Optional

from app.aggregation.prompts.extraction_prompts import build_pdf_extraction_prompt

logger = logging.getLogger("pdf_utils")

def is_parts_list_pdf(pdf_text: str) -> bool:
    
    if not pdf_text or len(pdf_text.strip()) < 100:
        return False
    
    pdf_lower = pdf_text.lower()
    part_list_patterns = [
        r'(?:part\s*(?:no|number|#)|fig\.?)\s*(?:description|part name|qty|no\.?\s*req)',
        r'(?:service|spare|replacement|exploded)\s*(?:parts?\s*(?:list|manual|breakdown|catalog)|view)',
        r'\bparts?\s*(?:list|manual|breakdown|catalog)\b',
        r'\b(?:qty|quantity|no\.?\s*req|part\s*no)\b.*\b(?:qty|quantity|no\.?\s*req|part\s*no)\b',
    ]
    
    is_parts = any(re.search(p, pdf_lower[:1000]) for p in part_list_patterns)
    if is_parts:
        logger.warning("PDF identified as parts list/exploded view - skipping")
    
    return is_parts
CROSSREF_KEYWORDS = [
    "cross reference", "cross-reference", "interchange",
    "replaces", "equivalent to", "compatible with",
    "oem cross", "supersedes", "alternate part"
]

def is_crossref_pdf(pdf_text: str, min_hits: int = 2) -> bool:
    text_lower=pdf_text.lower()
    hits=sum(1 for kw in CROSSREF_KEYWORDS if kw in text_lower)
    return hits>=min_hits

def _build_pdf_prompt(
    pdf_text: str,
    title: str,
    mpn: str,
    brand: Optional[str] = None,
    taxonomy: Optional[str] = None,
    primary_attributes: Optional[List[str]] = None,
    attribute_chunk: Optional[List[str]] = None
):
    
    attrs_to_use = primary_attributes or []
    if attribute_chunk:
        other_attrs = [a for a in attrs_to_use if a not in attribute_chunk]
        attrs_to_use = attribute_chunk + other_attrs
    
    return build_pdf_extraction_prompt(
        product_name=title,
        mpn=mpn,
        brand=brand or "",
        taxonomy=taxonomy or "",
        primary_attributes=attrs_to_use,
        pdf_text=pdf_text,
    )