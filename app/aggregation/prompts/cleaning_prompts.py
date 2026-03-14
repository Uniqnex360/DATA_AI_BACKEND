import logging
logger=logging.getLogger('build_cleaning_prompt')
def build_cleaning_prompt(raw_attributes: list) -> dict:
    try:
        attrs_text = "\n".join([
        f"  • {attr['name']}: {attr['value']} {attr.get('unit', '')}"
        for attr in raw_attributes
        ])
        prompt = f"""You are a product data cleaning engine.
            RAW ATTRIBUTES (from extraction):
            {attrs_text}
            CLEANING TASKS:
            1. Remove invalid/placeholder values:
            - "N/A", "-", "TBD", "Unknown", "See datasheet"
            - "Contact us", "Call for details"
            - Empty strings, "null", "None"
            2. Remove marketing language:
            - "Premium", "Best-in-class", "Industry-leading"
            - Superlatives that aren't measurable
            3. Normalize formatting:
            - Standardize units: "5kg" → "5 kg"
            - Remove extra whitespace
            - Fix casing: "IP67" not "ip67" or "Ip67"
            4. Deduplicate:
            - If same value appears multiple times, keep once
            5. Flag suspicious values:
            - Values that look like model numbers (wrong product)
            - Values that don't match attribute name
            KEEP:
            - Technical specifications with numbers
            - Material names
            - Standards/certifications
            - Dimensional data
            - All valid technical values
            OUTPUT:
            For each cleaned attribute, specify what cleaning was applied.
            If an attribute is completely removed, include it in "removed_count".
            Return JSON following CleaningResponse schema.
            """
        return {
        "prompt": prompt,
        "response_schema": "CleaningResponse",
        "max_tokens": 2000
        }
    except Exception as e:
        logger.error(f"Failed to generate  cleaning prompt :{str(e)}")
        return
    