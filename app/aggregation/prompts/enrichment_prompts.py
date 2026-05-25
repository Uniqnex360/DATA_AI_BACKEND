import logging
from typing import Optional
logger = logging.getLogger('build_enrichment_prompt')

def build_enrichment_prompt(
    golden_attributes: dict,
    product_name: str,
    brand: str,
    taxonomy: str,
    existing_short_description: Optional[str] = None,
    existing_long_description: Optional[str] = None
) -> dict:
    try:
        attrs_text = "\n".join([
            f"  • {attr['name']}: {attr['value']} {attr.get('unit', '')}"
            for attr in golden_attributes
        ])
        
        if existing_short_description or existing_long_description:
            final_short_description = existing_short_description or ""
            final_long_description = existing_long_description or ""
            
            prompt = f"""You are a product marketing content generator.

PRODUCT:
- Name: {product_name}
- Brand: {brand}
- Category: {taxonomy}

VERIFIED SPECIFICATIONS:
{attrs_text}

EXISTING PRODUCT DESCRIPTIONS (FROM MANUFACTURER WEBSITE - USE AS IS):
SHORT DESCRIPTION:
{final_short_description}

LONG DESCRIPTION:
{final_long_description}

TASK: 
- Use the EXISTING descriptions above EXACTLY as provided
- Do NOT modify, enhance, or rewrite them
- Return them as-is in the JSON response
- Also generate features from the verified specifications

Return JSON:
{{
    "short_description": "{final_short_description}",
    "long_description": "{final_long_description}",
    "features": ["feature 1", "feature 2", ...]
}}

RULES for features:
- Each feature must reference a real specification
- 5-8 bullet points
- Format: "Feature Name: Brief explanation with spec"
"""
        else:
            # No existing description - generate from specs
            prompt = f"""You are a product marketing content generator.

PRODUCT:
- Name: {product_name}
- Brand: {brand}
- Category: {taxonomy}

VERIFIED SPECIFICATIONS:
{attrs_text}

TASK: Generate marketing content based ONLY on verified specs above.

GENERATE:
1. Short Description (max 500 chars):
   - Professional tone
   - Highlight key benefits
   - No superlatives without data

2. Long Description (max 1000 chars):
   - Technical yet accessible
   - Explain how specs translate to benefits
   - Use specific numbers from specs

3. Key Features (5-8 bullet points):
   - Each feature must reference a real specification
   - Format: "Feature Name: Brief explanation with spec"

RULES:
- Do NOT invent features not supported by specs
- Do NOT use vague claims ("best", "premium") without data
- Use technical terms correctly

Return JSON following EnrichmentResponse schema.
"""

        return {
            "prompt": prompt,
            "response_schema": "EnrichmentResponse",
            "max_tokens": 1500
        }
    except Exception as e:
        logger.error(f"build_enrichment_prompt failed: {e}")
        raise e