from .llm import call_llm
from app.schemas.enrichment import EnrichmentResult
import json

def enrich_product(brand: str, category: str, standardized_attributes: dict) -> EnrichmentResult:
    prompt = f'''You are an expert e-commerce copywriter. Generate ONLY valid JSON. No explanations.

Brand: {brand}
Category: {category}
Confirmed specs (use ONLY these):
{json.dumps(standardized_attributes, indent=2)}

Generate exactly this JSON structure:

{{
  "seo_title": "string (max 80 chars)",
  "bullets": ["5 bullet points", "each under 100 chars"],
  "tags": ["5-8 keywords"],
  "use_cases": ["2-4 use cases"],
  "confidence": 0.95
}}

Do it now.'''

    schema = {
        "type": "object",
        "properties": {
            "seo_title": {"type": ["string", "null"]},
            "bullets": {"type": "array", "items": {"type": "string"}, "minItems": 5, "maxItems": 5},
            "tags": {"type": "array", "items": {"type": "string"}, "minItems": 5, "maxItems": 10},
            "use_cases": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 4},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0}
        },
        "required": ["bullets", "tags", "use_cases", "confidence"],
        "additionalProperties": False
    }

    result = call_llm(prompt, schema)
    return EnrichmentResult(**result)