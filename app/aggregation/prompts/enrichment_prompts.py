

import logging
logger=logging.getLogger('build_enrichment_prompt')
def build_enrichment_prompt(golden_attributes: dict,product_name: str,brand: str,taxonomy: str) -> dict:
    try:
        attrs_text = "\n".join([
        f"  • {attr['name']}: {attr['value']} {attr.get('unit', '')}"
        for attr in golden_attributes
    ])

        prompt = f"""You are a product marketing content generator.

    PRODUCT:
    - Name: {product_name}
    - Brand: {brand}
    - Category: {taxonomy}

    VERIFIED SPECIFICATIONS:
    {attrs_text}

    TASK: Generate marketing content based ONLY on verified specs above.

    GENERATE:
    1. Short Description (150-200 chars):
    - Professional tone
    - Highlight key benefits
    - No superlatives without data

    2. Long Description (500-800 words):
    - Technical yet accessible
    - Explain how specs translate to benefits
    - Use specific numbers from specs

    3. Key Features (5-8 bullet points):
    - Each feature must reference a real specification
    - Format: "Feature Name: Brief explanation with spec"
    - Example: "Robust Construction: IP67-rated enclosure withstands harsh environments"

    RULES:
    - Do NOT invent features not supported by specs
    - Do NOT use vague claims ("best", "premium") without data
    - Do NOT contradict any specification
    - Use technical terms correctly
    - Maintain professional tone throughout

    Return JSON following EnrichmentResponse schema.
    """

        return {
            "prompt": prompt,
            "response_schema": "EnrichmentResponse",
            "max_tokens": 1500
        }
    except Exception as e:
        logger.error(f"build_enrichment_prompt")
        raise e

    
