import json
import logging
from typing import List, Optional

from app.aggregation.prompts.extraction_prompts import SYMBOL_STRIPPING_RULE
logger = logging.getLogger('build_enrichment_prompt')


def build_enrichment_prompt(
    golden_attributes: dict,
    product_name: str,
    brand: str,
    taxonomy: str,
    existing_short_description: Optional[str] = None,
    existing_long_description: Optional[str] = None,
    existing_features: Optional[List[str]] = None
) -> dict:
    try:
        attrs_text = "\n".join([
            f"  • {attr['name']}: {attr['value']} {attr.get('unit', '')}"
            for attr in golden_attributes
        ])

        if existing_short_description or existing_long_description or existing_features:
            final_short_description = existing_short_description or ""
            final_long_description = existing_long_description or ""
            final_features = existing_features or []

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
            - Use the EXISTING long_description and short_description exactly as provided.
            - If features are provided, return them exactly as provided.
            - If no features are provided by the manufacturer, return an empty list [].
            - DO NOT generate any features yourself under any condition.

            Return JSON:
            {{
                "short_description": "{final_short_description}",
                "long_description": "{final_long_description}",
                "features": {json.dumps(final_features)}
            }}
                        """
        else:
            prompt = f"""You are a product marketing content generator.

            PRODUCT:
            - Name: {product_name}
            - Brand: {brand}
            - Category: {taxonomy}

            VERIFIED SPECIFICATIONS:
            {attrs_text}

            TASK: Generate marketing content based ONLY on verified specs above.

            GENERATE:
            1. Short Description 
            2. Long Description

            Features: Return an empty list [] only. Do NOT generate any features.
            {SYMBOL_STRIPPING_RULE}

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
