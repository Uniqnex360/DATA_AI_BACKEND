import re
import logging

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