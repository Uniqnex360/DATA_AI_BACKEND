import logging
logger=logging.getLogger('build_unification_prompt')
def build_unification_prompt(
    cleaned_attributes: list,
    taxonomy: str,
    mpn: str
) -> dict:
    try:
        attr_names = list(set([attr['name'] for attr in cleaned_attributes]))
        prompt = f"""You are a semantic attribute unification engine.
    PRODUCT CONTEXT (CRITICAL):
    - Category: {taxonomy}
    - Model: {mpn}
    This context is ESSENTIAL because:
    - "Size" means different things for different products
    - TV: screen diagonal
    - Clothing: garment size
    - Fastener: thread diameter
    ATTRIBUTE NAMES TO UNIFY:
    {chr(10).join([f"  • {name}" for name in attr_names])}
    UNIFICATION RULES:
    1. Group attributes that refer to the SAME technical specification
    2. Use product category to disambiguate
    3. Choose the most standard/common name as canonical
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
    