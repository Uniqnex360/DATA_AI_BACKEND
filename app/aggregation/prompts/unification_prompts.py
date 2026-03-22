import logging
from typing import Optional,List
logger=logging.getLogger('build_unification_prompt')
def build_unification_prompt(
    cleaned_attributes: list,
    taxonomy: str,
    mpn: str,
    expected_attributes:Optional[List[str]]=None
) -> dict:
    try:
        attr_names = list(set([attr['name'] for attr in cleaned_attributes]))
        expected_section = ""
        if expected_attributes:
            expected_section = f"""
        EXPECTED ATTRIBUTE NAMES (use these as canonical when possible):
        {chr(10).join([f"  • {name}" for name in expected_attributes])}
        """
        synonym_hints = ""
        if "lighting" in taxonomy.lower():
            synonym_hints = """
        SPECIAL RULES FOR LIGHTING PRODUCTS:
        - "CCT" and "Color Temperature" are the same attribute. Use "Color Temperature" as the canonical name.
        - "Lumens", "Luminous Flux", and "Light Output" are the same. Use "Lumens" as canonical.
        - "CRI" and "Color Rendering Index" are the same. Use "CRI" as canonical.
        """
        elif "safety" in taxonomy.lower() or "fall protection" in taxonomy.lower():
            synonym_hints = """
        SPECIAL RULES FOR SAFETY HARNESSES:
        - Any attribute that describes the chest buckle type (e.g., "Buckle Type", "Chest Buckle Type", "Buckle Type - Chest") should be unified into a single attribute with the canonical name **"Buckle Type - Chest"**.
        - "Number of D-Rings" is the canonical name (do not merge with other counts).
        """
        prompt = f"""You are a semantic attribute unification engine.
    PRODUCT CONTEXT (CRITICAL):
    - Category: {taxonomy}
    - Model: {mpn}
    This context is ESSENTIAL because:
    - "Size" means different things for different products
    - TV: screen diagonal
    - Clothing: garment size
    - Fastener: thread diameter
    {synonym_hints}
    {expected_section}

    ATTRIBUTE NAMES TO UNIFY:
    {chr(10).join([f"  • {name}" for name in attr_names])}
    UNIFICATION RULES:
    1. Group attributes that refer to the SAME technical specification
    2. Use product category to disambiguate
    3. Choose the most standard/common name as canonical – **if an expected attribute name is available, prefer it**.
    4. For each group, explain WHY these attributes are the same.
    
    Examples for category "{taxonomy}":
    - ["Display Size", "Screen Size", "LCD Diagonal"] → "Display Size"
    - ["Power", "Wattage", "Power Consumption"] → "Power Consumption"
    - ["Weight", "Mass", "Product Weight"] → "Weight"
    DO NOT group:
    - Attributes that are similar but distinct
    (e.g., "Input Voltage" ≠ "Output Voltage")
    - Attributes from different specification domains
    (e.g., "Operating Temperature" ≠ "Storage Temperature")
    For each group, explain WHY these attributes are the same thing.
    Return JSON following UnificationResponse schema.
    """
        return {
            "prompt": prompt,
            "response_schema": "UnificationResponse",
            "max_tokens": 1500
        }
    except Exception as e:
        logger.error(f"build_unification_prompt failed :{str(e)}")
    