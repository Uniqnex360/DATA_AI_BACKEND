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


# def extract_from_web(html: str) -> Dict:
#     if not html or len(html.strip()) < 100:
#         logger.warning("Web HTML too short or empty")
#         return {"source": "web", "attributes": {}, "error": "empty_html"}

#     prompt = f"""
#     You are a Technical Data Extractor. 
#     Extract every single technical specification, dimension, material, and warranty detail from this HTML.
# Look for tables, list items (li), and definition lists (dt/dd).
# Extract ALL product attributes exactly as written from this HTML.
# Rules: - Do not normalize - Do not merge - Do not interpret - Keep original labels
# HTML (first 12000 chars):
# {html[:12000]}

# Output ONLY JSON: {{"source": "web", "attributes": {{"Attribute Name": "Value"}}}}
# """
#     schema = {
#         "type": "object",
#         "properties": {
#             "source": {"type": "string", "const": "web"},
#             "attributes": {"type": "object"}
#         },
#         "required": ["source", "attributes"]
#     }
#     result = safe_call_llm(prompt, schema, "extract_from_web")
#     return result if "attributes" in result else {"source": "web", "attributes": {}, "error": "extraction_failed"}
def fallback_extraction(html: str) -> Dict:
    """Universal fallback extraction - no product assumptions"""
    from bs4 import BeautifulSoup
    import re
    
    attributes = {}
    
    try:
        soup = BeautifulSoup(html, 'html.parser')
        
        # Strategy 1: ALL tables (filter by quality later)
        for table in soup.find_all('table'):
            for row in table.find_all('tr'):
                cells = row.find_all(['td', 'th'])
                if len(cells) == 2:  # Key-value pair
                    key = cells[0].get_text(strip=True).rstrip(':')
                    val = cells[1].get_text(strip=True)
                    if key and val and 2 < len(key) < 100 and len(val) < 500:
                        attributes[key] = val
        
        # Strategy 2: Definition lists
        for dl in soup.find_all('dl'):
            dts = dl.find_all('dt')
            dds = dl.find_all('dd')
            for dt, dd in zip(dts, dds):
                key = dt.get_text(strip=True).rstrip(':')
                val = dd.get_text(strip=True)
                if key and val and len(key) < 100:
                    attributes[key] = val
        
        # Strategy 3: Colon-separated patterns (common in product specs)
        # Look for patterns like "Weight: 500g" or "Material: Plastic"
        text_blocks = soup.find_all(['p', 'li', 'div', 'span'])
        for block in text_blocks:
            text = block.get_text()
            # Match "Label: Value" patterns
            matches = re.findall(r'([A-Za-z][A-Za-z\s]{2,50}):\s*([^\n:]{1,200})', text)
            for key, val in matches:
                key = key.strip()
                val = val.strip()
                if key and val and not key.lower().startswith(('http', 'www')):
                    attributes[key] = val
        
        # Strategy 4: Meta tags (sometimes contain specs)
        for meta in soup.find_all('meta'):
            if meta.get('property') and meta.get('content'):
                prop = meta['property']
                if 'product' in prop.lower():
                    key = prop.split(':')[-1].replace('_', ' ').title()
                    attributes[key] = meta['content']
        
        # Strategy 5: JSON-LD structured data
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                import json
                data = json.loads(script.string)
                if isinstance(data, dict):
                    # Extract product properties
                    if 'Product' in data.get('@type', ''):
                        for key, val in data.items():
                            if key not in ['@context', '@type'] and isinstance(val, (str, int, float)):
                                attributes[key.title()] = str(val)
            except:
                pass
        
        logger.info(f"Fallback extraction found {len(attributes)} attributes")
        return attributes
        
    except Exception as e:
        logger.error(f"Fallback extraction error: {e}")
        return {}
def extract_from_web(html: str, sku: str = "") -> Dict:
    """Two-pass extraction: discover schema, then extract"""
    if not html or len(html.strip()) < 100:
        logger.warning("Web HTML too short or empty")
        return {"source": "web", "attributes": {}, "error": "empty_html"}

    # PASS 1: Schema Discovery
    discovery_result = discover_attributes(html, sku)
    
    if not discovery_result or not discovery_result.get("found_attributes"):
        logger.warning(f"No attributes discovered for {sku}, using fallback")
        return {
            "source": "web",
            "attributes": fallback_extraction(html),
            "extraction_method": "fallback"
        }
    
    # PASS 2: Targeted Extraction
    extraction_result = extract_discovered_attributes(
        html, 
        discovery_result["found_attributes"],
        sku
    )
    
    return extraction_result


def discover_attributes(html: str, sku: str = "") -> Dict:
    """Pass 1: Discover what attributes exist in the HTML"""
    
    prompt = f"""
You are analyzing an HTML product page to discover what technical specifications exist.

Your job: Identify ALL attribute names/labels that appear in the HTML, especially in:
- Table headers or row labels
- Definition list terms (<dt>)
- Labels before colons (e.g., "Battery Capacity:", "Material:", "Dimensions:")
- Section headings containing "specifications", "details", "features", "tech specs"

Do NOT extract values yet - only find the attribute NAMES.

HTML (first 10000 chars):
{html[:10000]}

Output ONLY JSON:
{{
  "found_attributes": ["attribute name 1", "attribute name 2", ...],
  "product_type_hint": "brief description of what this product appears to be"
}}

Examples of attribute names: "Battery Capacity", "Weight", "Material", "Color", "SKU", "Warranty Period"
"""

    schema = {
        "type": "object",
        "properties": {
            "found_attributes": {
                "type": "array",
                "items": {"type": "string"}
            },
            "product_type_hint": {"type": "string"}
        },
        "required": ["found_attributes"]
    }
    
    try:
        result = safe_call_llm(prompt, schema, "discover_attributes")
        logger.info(f"Discovered {len(result.get('found_attributes', []))} attributes for {sku}: {result.get('product_type_hint', 'unknown')}")
        return result
    except Exception as e:
        logger.error(f"Schema discovery failed for {sku}: {e}")
        return {"found_attributes": [], "error": str(e)}


def extract_discovered_attributes(html: str, attribute_names: list, sku: str = "") -> Dict:
    """Pass 2: Extract specific attributes discovered in pass 1"""
    
    if not attribute_names:
        return {"source": "web", "attributes": {}, "error": "no_attributes_discovered"}
    
    prompt = f"""
You are extracting specific technical specifications from HTML.

Extract the VALUES for these attributes (if they exist in the HTML):
{', '.join(attribute_names[:50])}  # Limit to first 50 to avoid token limits

Rules:
- Extract EXACTLY as written (preserve units, formatting, capitalization)
- If an attribute appears multiple times, use the most detailed/complete value
- If an attribute is not found, omit it (don't include null values)
- Look in tables, lists, divs, and any structured data

HTML (first 15000 chars):
{html[:15000]}

Output ONLY JSON: {{"source": "web", "attributes": {{"Attribute Name": "value"}}}}
"""

    schema = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "const": "web"},
            "attributes": {"type": "object"}
        },
        "required": ["source", "attributes"]
    }
    
    try:
        result = safe_call_llm(prompt, schema, "extract_discovered_attributes")
        
        # Validate we got real data
        if not result or "attributes" not in result:
            logger.warning(f"Extraction failed for {sku}")
            return {"source": "web", "attributes": {}, "error": "extraction_failed"}
        
        # Check if all values are null/empty
        attrs = result["attributes"]
        if not attrs or all(v is None or v == "" for v in attrs.values()):
            logger.warning(f"All extracted values are null/empty for {sku}, trying fallback")
            return {
                "source": "web",
                "attributes": fallback_extraction(html),
                "extraction_method": "fallback"
            }
        
        # Filter out null values
        result["attributes"] = {k: v for k, v in attrs.items() if v is not None and v != ""}
        logger.info(f"Successfully extracted {len(result['attributes'])} attributes for {sku}")
        
        return result
        
    except Exception as e:
        logger.exception(f"Attribute extraction failed for {sku}: {e}")
        return {"source": "web", "attributes": {}, "error": str(e)}
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


def unify_attributes(attributes: List[str]):
    prompt = f"""
You are a semantic attribute harmonization engine.
Raw attribute names from multiple sources:
{attributes}

Task:
- Identify which attributes mean the same thing
- Group them under ONE canonical attribute in snake_case
- Do NOT invent new attributes
- Return only valid JSON

Example output:
{{
  "canonical_attributes": {{
    "screen_size": {{
      "synonyms": ["Display Size", "Screen Size", "Diagonal", "Size"],
      "confidence": 0.99
    }},
    "ip_rating": {{
      "synonyms": ["Water Rating", "Waterproof Rating", "Ingress Protection"],
      "confidence": 0.97
    }}
  }}
}}
"""

    schema = {
        "name": "unification",
        "schema": {
            "type": "object",
            "properties": {
                "canonical_attributes": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "object",
                        "properties": {
                            "synonyms": {"type": "array", "items": {"type": "string"}},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1}
                        },
                        "required": ["synonyms", "confidence"]
                    }
                }
            },
            "required": ["canonical_attributes"]
        }
    }

    result = call_llm(prompt, schema)
    return result


# def build_golden_record(standarized_data: Dict, identifiers: Dict) -> Dict:
#     if not identifiers or 'mpn' not in identifiers:
#         logger.error("Golder record failed:missing identifiers")
#         return {
#             'sku': identifiers.get('mpn', 'UNKNOWN'),
#             'brand': identifiers.get('brand', 'UNKNOWN'),
#             'attributes': {},
#             "ready_for_publish": False,
#             "error": 'missing_identifiers',
#             'sources': []
#         }
#     if not standarized_data:
#         logger.warning("Golden record:no standarized data")
#         return {
#             'sku': identifiers.get('mpn', 'UNKNOWN'),
#             'brand': identifiers.get('brand', 'UNKNOWN'),
#             'attributes': {},
#             "ready_for_publish": False,
#             "error": 'missing_identifiers',
#             'sources': []
#         }
#     prompt = f""" 
#     You are the final arbiter of truth.
#     Create a clean JSON Golden record using ONLY the provided standardized data.
#     NEVER invent information. 
#     Identifiers:{json.dumps(identifiers)}
#     Standarized attributes (TRUTH):{json.dumps(standarized_data, indent=2)}
#     Rules:
#     - Use ONLY data from above
#     - ready_for_publish = true IF you have the Brand AND at least 4 other valid technical specifications.
#     - If uncertain -> ready_for_publish=false
    
#     Return exactly this structure
#     """
#     schema = {
#         'type': 'object',
#         'properties': {
#             'sku': {'type': 'string'},
#             'brand': {'type': 'string'},
#             'attributes': {"type": "object"},
#             "ready_for_publish": {'type': 'boolean'},
#             'sources': {'type': "array", 'items': {"type": 'string'}},
#             'confidence': {'type': "number", 'minimum': 0, "maximum": 1}
#         },
#         'required': ['sku', 'brand', 'attributes', 'ready_for_publish'],
#         "additionalProperties": False
#     }
#     result = safe_call_llm(prompt, schema, 'built_golden_record')
#     if 'error' in result or not result.get('attributes'):
#         logger.warning(
#             f"Golden record LLM failed,using deterministic fallback for {identifiers.get('mpn')}")
#         return {
#             'sku': identifiers.get('mpn', 'UNKNOWN'),
#             'brand': identifiers.get('brand', 'UNKNOWN'),
#             'attributes': standarized_data,
#             "ready_for_publish": len(standarized_data) >= 4,
#             "error": 'missing_identifiers',
#             'sources': [],
#             'confidence': 0.5,
#             'generated_by': 'deterministic_fallback'
#         }
#     return result
def build_golden_record(standardized_data: Dict, identifiers: Dict) -> Dict:
    """Build final golden record from standardized data"""
    
    if not identifiers or 'mpn' not in identifiers:
        logger.error("Golden record failed: missing identifiers")
        return {
            'sku': identifiers.get('mpn', 'UNKNOWN'),
            'brand': identifiers.get('brand', 'UNKNOWN'),
            'attributes': {},
            'ready_for_publish': False,
            'error': 'missing_identifiers'
        }
    
    if not standardized_data:
        logger.warning("Golden record: no standardized data")
        return {
            'sku': identifiers.get('mpn', 'UNKNOWN'),
            'brand': identifiers.get('brand', 'UNKNOWN'),
            'attributes': {},
            'ready_for_publish': False,
            'error': 'no_standardized_data'
        }
    
    tech_spec_count = len(standardized_data)
    has_brand = bool(identifiers.get('brand'))
    
    prompt = f"""
Create a product Golden Record and return the result as JSON.

INPUT DATA:
SKU/MPN: {identifiers.get('mpn')}
Brand: {identifiers.get('brand')}

STANDARDIZED ATTRIBUTES:
{json.dumps(standardized_data, indent=2)}

YOUR TASK:
Create a clean JSON object with:
1. Copy the SKU and brand from above
2. Include ALL standardized attributes 
3. Set ready_for_publish based on: has brand ({has_brand}) AND at least 4 specs ({tech_spec_count} found)
4. Assign confidence 0.0-1.0 based on data completeness

Return ONLY this JSON structure (no markdown, no extra text):
{{
  "sku": "the SKU value",
  "brand": "the brand value",
  "attributes": {{
    "attribute_name": "value",
    ...all attributes from STANDARDIZED ATTRIBUTES...
  }},
  "ready_for_publish": true or false,
  "confidence": 0.0 to 1.0
}}

CRITICAL: The response must be valid JSON only. Do not add "identifiers" or "standardized_attributes" as keys.
"""
    
    schema = {
        'type': 'object',
        'properties': {
            'sku': {'type': 'string'},
            'brand': {'type': 'string'},
            'attributes': {'type': 'object'},
            'ready_for_publish': {'type': 'boolean'},
            'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1}
        },
        'required': ['sku', 'brand', 'attributes', 'ready_for_publish'],
        'additionalProperties': False
    }
    
    try:
        result = safe_call_llm(prompt, schema, 'build_golden_record')
        
        # Validate result
        if not result or 'error' in result:
            raise ValueError(f"LLM returned error: {result.get('error', 'unknown')}")
        
        # Check for required fields
        missing = [f for f in ['sku', 'brand', 'attributes', 'ready_for_publish'] 
                   if f not in result]
        if missing:
            raise ValueError(f"Missing required fields: {missing}")
        
        # Check attributes not empty
        if not result.get('attributes'):
            raise ValueError("Empty attributes")
        
        # Check for wrong structure (nested keys)
        if any(k in result for k in ['identifiers', 'standardized_attributes', 'product_attributes']):
            raise ValueError("LLM returned nested structure")
        
        logger.info(
            f"✓ Golden record for {result['sku']}: "
            f"{len(result['attributes'])} attrs, "
            f"ready={result['ready_for_publish']}"
        )
        
        return result
        
    except Exception as e:
        logger.warning(
            f"Golden record LLM failed for {identifiers.get('mpn')}: {e}, "
            f"using deterministic fallback"
        )
        
        # Deterministic fallback
        return {
            'sku': identifiers.get('mpn', 'UNKNOWN'),
            'brand': identifiers.get('brand', 'UNKNOWN'),
            'attributes': standardized_data,
            'ready_for_publish': has_brand and tech_spec_count >= 4,
            'confidence': 0.7 if tech_spec_count >= 5 else 0.5,
            'generated_by': 'deterministic_fallback',
            'reason': str(e)
        }