import json
import logging
from typing import Dict, List, Any, Optional
from .llm import call_llm

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aggregation_engine")


def safe_call_llm(prompt: str, schema: dict, context: str = "") -> dict:
    if not prompt.strip():
        logger.warning(f"Empty prompt in {context}")
        return {"error": "empty_prompt", "context": context}

    try:
        result = call_llm(prompt, schema)
        if not isinstance(result, dict):
            logger.error(f"LLM returned non-dict in {context}: {result}")
            return {"error": "invalid_response", "raw": str(result)}
        return result
    except Exception as e:
        logger.error(f"LLM FAILED in {context}: {e}")
        return {"error": "llm_exception", "details": str(e)}


def generate_search_queries(mpn: str = None, brand: str = None, title: str = None) -> List[str]:
    if not any([mpn, brand, title]):
        logger.warning("No identifiers provided for search queries")
        return []

    prompt = f"""
    Generate 5 highly targeted Google search queries to find technical specifications for this product.
Input: {json.dumps({"mpn": mpn, "brand": brand, "title": title}, ensure_ascii=False)}
Output ONLY valid JSON with key 'queries' as array of strings.
"""
    schema = {
        "type": "object",
        "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
        "required": ["queries"]
    }
    result = safe_call_llm(prompt, schema, "generate_search_queries")
    return result.get("queries", [])


def extract_from_web(html: str) -> Dict:
    if not html or len(html.strip()) < 100:
        logger.warning("Web HTML too short or empty")
        return {"source": "web", "attributes": {}, "error": "empty_html"}

    prompt = f"""
Extract ALL product attributes exactly as written from this HTML.
Rules: - Do not normalize - Do not merge - Do not interpret - Keep original labels
HTML (first 12000 chars):
{html[:12000]}

Output ONLY JSON: {{"source": "web", "attributes": {{"Attribute Name": "Value"}}}}
"""
    schema = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "const": "web"},
            "attributes": {"type": "object"}
        },
        "required": ["source", "attributes"]
    }
    result = safe_call_llm(prompt, schema, "extract_from_web")
    return result if "attributes" in result else {"source": "web", "attributes": {}, "error": "extraction_failed"}


def extract_from_pdf(text: str) -> Dict:
    if not text.strip():
        return {"source": "pdf", "attributes": {}, "error": "empty_pdf"}

    prompt = f"""
Extract technical specifications from this PDF text.
Rules: - Extract tables, bullet specs, compliance data - Keep original wording - No assumptions
Text (first 12000 chars):
{text[:12000]}

Output ONLY JSON: {{"source": "pdf", "attributes": {{"Spec Name": "Value"}}}}
"""
    schema = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "const": "pdf"},
            "attributes": {"type": "object"}
        },
        "required": ["source", "attributes"]
    }
    result = safe_call_llm(prompt, schema, "extract_from_pdf")
    return result


def extract_from_image(description: str) -> Dict:
    if not description.strip():
        return {"source": "image", "metadata": {"text_detected": []}, "error": "no_description"}

    prompt = f"""
Analyze this product image description. Extract only visible text.
Do not guess specifications.
Description: {description}

Output ONLY JSON.
"""
    schema = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "const": "image"},
            "metadata": {
                "type": "object",
                "properties": {
                    "resolution": {"type": "string"},
                    "background": {"type": "string"},
                    "text_detected": {"type": "array", "items": {"type": "string"}}
                }
            }
        },
        "required": ["source", "metadata"]
    }
    return safe_call_llm(prompt, schema, "extract_from_image")


def aggregate_per_canonical(canonical: str, values: List[Dict]) -> Dict:
    if not values:
        return {canonical: {"values": [], "conflict": False}}

    prompt = f"""
Aggregate values for canonical attribute '{canonical}'.
Raw values: {json.dumps(values)}

Rules:
- Keep all raw values
- Preserve source
- conflict = True only if values differ meaningfully (e.g. 12 vs 13)
- "12 inch" vs "12\"" → conflict = False

Return ONLY JSON.
"""
    schema = {
        "type": "object",
        "properties": {
            canonical: {
                "type": "object",
                "properties": {
                    "values": {"type": "array"},
                    "conflict": {"type": "boolean"}
                },
                "required": ["values", "conflict"]
            }
        },
        "required": [canonical]
    }
    result = safe_call_llm(prompt, schema, f"aggregate_{canonical}")
    return result.get(canonical, {"values": values, "conflict": True})


def standardize_with_llm(attribute: str, values: List[str]) -> dict:
    if not values:
        return {"standard_value": None, "unit": None, "derived_from": []}

    prompt = f"""
Standardize attribute: {attribute}
Values: {json.dumps(values)}
Rules: Convert units, enforce enums, pick one truth.
Output ONLY JSON.
"""
    schema = {
        "type": "object",
        "properties": {
            "standard_value": {},
            "unit": {"type": ["string", "null"]},
            "derived_from": {"type": "array"}
        },
        "required": ["standard_value", "derived_from"]
    }
    return safe_call_llm(prompt, schema, f"standardize_{attribute}")


def build_golden_record(standarized_data: Dict, identifiers: Dict) -> Dict:
    if not identifiers or 'mpn' not in identifiers:
        logger.error("Golder record failed:missing identifiers")
        return {
            'sku': identifiers.get('mpn', 'UNKNOWN'),
            'brand': identifiers.get('brand', 'UNKNOWN'),
            'attributes': {},
            "ready_for_publish": False,
            "error": 'missing_identifiers',
            'sources': []
        }
    if not standarized_data:
        logger.warning("Golden record:no standarized data")
        return {
            'sku': identifiers.get('mpn', 'UNKNOWN'),
            'brand': identifiers.get('brand', 'UNKNOWN'),
            'attributes': {},
            "ready_for_publish": False,
            "error": 'missing_identifiers',
            'sources': []
        }
    prompt = f""" 
    You are the final arbiter of truth.
    Create a clean JSON Golden record using ONLY the provided standardized data.
    NEVER invent information. 
    Identifiers:{json.dumps(identifiers)}
    Standarized attributes (TRUTH):{json.dumps(standarized_data, indent=2)}
    Rules:
    - Use ONLY data from above
    - ready_for_publish = true IF you have the Brand AND at least 4 other valid technical specifications.
    - If uncertain -> ready_for_publish=false
    
    Return exactly this structure
    """
    schema = {
        'type': 'object',
        'properties': {
            'sku': {'type': 'string'},
            'brand': {'type': 'string'},
            'attributes': {"type": "object"},
            "ready_for_publish": {'type': 'boolean'},
            'sources': {'type': "array", 'items': {"type": 'string'}},
            'confidence': {'type': "number", 'minimum': 0, "maximum": 1}
        },
        'required': ['sku', 'brand', 'attributes', 'ready_for_publish'],
        "additionalProperties": False
    }
    result = safe_call_llm(prompt, schema, 'built_golden_record')
    if 'error' in result or not result.get('attributes'):
        logger.warning(
            f"Golden record LLM failed,using deterministic fallback for {identifiers.get('mpn')}")
        return {
            'sku': identifiers.get('mpn', 'UNKNOWN'),
            'brand': identifiers.get('brand', 'UNKNOWN'),
            'attributes': standarized_data,
            "ready_for_publish": len(standarized_data) >= 4,
            "error": 'missing_identifiers',
            'sources': [],
            'confidence': 0.5,
            'generated_by': 'deterministic_fallback'
        }
    return result
