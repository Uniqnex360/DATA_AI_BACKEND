from .llm import call_llm
from app.schemas.enrichment import EnrichmentResult
import json

ENRICHMENT_SYSTEM_PROMPT = """You are a product data enrichment engine.

Input:
- Approved standardized product attributes
- Brand name
- Product category (if available)

ENRICHMENT RULES (CRITICAL):
1. Do NOT create or infer technical specifications
2. Do NOT modify standardized attributes
3. Use factual attributes only
4. If data is missing → skip enrichment for that part
5. No guessing, no inference of specs
6. Always keep enrichment separate from core attributes

Tasks:
- Generate SEO-friendly product title
- Generate 4–6 bullet points
- Generate short tags/keywords
- Identify high-level use cases

Output structured JSON only."""

def enrich_product(brand: str, category: str, standardized_attributes: dict) -> EnrichmentResult:
    prompt = f"""{ENRICHMENT_SYSTEM_PROMPT}

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

Do it now. Output ONLY valid JSON. No explanations."""

    schema = {
        "type": "object",
        "properties": {
            "seo_title": {"type": ["string", "null"]},
            "bullets": {
                "type": "array", 
                "items": {"type": "string"}, 
                "minItems": 4, 
                "maxItems": 6  # Changed to 4-6 to match rules
            },
            "tags": {
                "type": "array", 
                "items": {"type": "string"}, 
                "minItems": 5, 
                "maxItems": 8  # Changed to 5-8 to match rules
            },
            "use_cases": {
                "type": "array", 
                "items": {"type": "string"}, 
                "minItems": 2, 
                "maxItems": 4
            },
            "confidence": {
                "type": "number", 
                "minimum": 0.0, 
                "maximum": 1.0
            }
        },
        "required": ["bullets", "tags", "use_cases", "confidence"],
        "additionalProperties": False
    }

    result = call_llm(prompt, schema)
    return EnrichmentResult(**result)