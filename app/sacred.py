from __future__ import annotations
import json
import logging
from typing import Dict, List, Any, Optional
from .llm import call_llm
import httpx
from typing import Optional
import re
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
    short_title = ""
    if title:
        words = str(title).split()
        short_title = " ".join(words[:5])
    if mpn and brand:
        core = f"{brand} {mpn}"
    elif mpn:
        core = mpn
    else:
        core = f"{brand or ''} {short_title}".strip()
    logger.info(f" Generating queries for core term: {core}")
    queries = [
        f"{core} technical specifications",
        f"{core} datasheet pdf",
        f"{core} official product page",
        f"{core} features and dimensions"
    ]
    known_domains = {
        "Sony": "sony.com",
        "Logitech": "logitech.com",
        "Adidas": "adidas.com",
        "Apple": "apple.com",
        "Samsung": "samsung.com",
    }
    brand_key = str(brand).strip().title() if brand else ""
    if brand_key in known_domains:
        domain = known_domains[brand_key]
        queries.insert(0, f"site:{domain} {core}")
    return queries


def fallback_extraction(html: str) -> Dict:
    from bs4 import BeautifulSoup
    import re
    attributes = {}
    try:
        soup = BeautifulSoup(html, 'html.parser')
        for table in soup.find_all('table'):
            for row in table.find_all('tr'):
                cells = row.find_all(['td', 'th'])
                if len(cells) == 2:
                    key = cells[0].get_text(strip=True).rstrip(':')
                    val = cells[1].get_text(strip=True)
                    if key and val and 2 < len(key) < 100 and len(val) < 500:
                        attributes[key] = val
        for dl in soup.find_all('dl'):
            dts = dl.find_all('dt')
            dds = dl.find_all('dd')
            for dt, dd in zip(dts, dds):
                key = dt.get_text(strip=True).rstrip(':')
                val = dd.get_text(strip=True)
                if key and val and len(key) < 100:
                    attributes[key] = val
        text_blocks = soup.find_all(['p', 'li', 'div', 'span'])
        for block in text_blocks:
            text = block.get_text()
            matches = re.findall(
                r'([A-Za-z][A-Za-z\s]{2,50}):\s*([^\n:]{1,200})', text)
            for key, val in matches:
                key = key.strip()
                val = val.strip()
                if key and val and not key.lower().startswith(('http', 'www')):
                    attributes[key] = val
        for meta in soup.find_all('meta'):
            if meta.get('property') and meta.get('content'):
                prop = meta['property']
                if 'product' in prop.lower():
                    key = prop.split(':')[-1].replace('_', ' ').title()
                    attributes[key] = meta['content']
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                import json
                data = json.loads(script.string)
                if isinstance(data, dict):
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


async def extract_image_from_source(source_html: str, source_url: str) -> Optional[str]:
    match = re.search(
        r'<meta[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', source_html)
    if not match:
        match = re.search(
            r'<meta[^>]*name=["\']twitter:image["\'][^>]*content=["\']([^"\']+)["\']', source_html)
    if not match:
        return None
    image_url = match.group(1)
    if image_url.startswith('//'):
        image_url = 'https:' + image_url
    if image_url.startswith('/'):
        from urllib.parse import urljoin
        image_url = urljoin(source_url, image_url)
    junk_keywords = ['logo', 'icon', 'pixel', 'banner',
                     'avatar', 'button', 'spacer', 'loading']
    if any(junk in image_url.lower() for junk in junk_keywords):
        return None
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=5.0) as client:
            response = await client.head(image_url)
            if response.status_code == 200:
                content_type = response.headers.get("content-type", "").lower()
                if "image" in content_type:
                    return image_url
            else:
                response = await client.get(image_url, headers={"Range": "bytes=0-100"})
                if response.status_code in [200, 206] and "image" in response.headers.get("content-type", ""):
                    return image_url
    except Exception as e:
        import logging
        logging.getLogger("truth_engine").warning(
            f"Image validation failed for {image_url}: {e}")
        return None
    return None


def extract_from_web(
    html: str,
    sku: str = "",
    taxonomy: str = None,
    custom_prompt: Optional[str] = None
) -> Dict:
    if not html or len(html.strip()) < 100:
        logger.warning("Web HTML too short or empty")
        return {"source": "web", "attributes": {}, "error": "empty_html"}
    if custom_prompt:
        logger.info(f" Using CUSTOM prompt for {sku}")
        full_prompt = f"""{custom_prompt}
        HTML CONTENT TO EXTRACT FROM:
        {html[:12000]}  
        Extract the requested attributes from this HTML content.
        """
        try:
            result = safe_call_llm(
                prompt=full_prompt,
                schema={
                    "type": "object",
                    "properties": {
                        "attributes": {
                            "type": "object",
                            "additionalProperties": True
                        },
                        "discovered_taxonomy": {"type": "string"}
                    },
                    "required": ["attributes"]
                },
                context="extract_from_web_custom"
            )
            if result and result.get("attributes"):
                logger.info(
                    f" Custom prompt extraction: {len(result['attributes'])} attributes found")
                return {
                    "source": "web",
                    "attributes": result["attributes"],
                    "extraction_method": "custom_prompt",
                    "discovered_taxonomy": result.get("discovered_taxonomy", taxonomy)
                }
            else:
                logger.warning(
                    f" Custom prompt returned no attributes, falling back")
        except Exception as e:
            logger.error(
                f" Custom prompt extraction failed: {e}, falling back")
    logger.info(f" Using DEFAULT discovery for {sku}")
    discovery_result = discover_attributes(html, sku, taxonomy)
    if not discovery_result or not discovery_result.get("found_attributes"):
        logger.warning(f"No attributes discovered for {sku}, using fallback")
        return {
            "source": "web",
            "attributes": fallback_extraction(html),
            "extraction_method": "fallback"
        }
    extraction_result = extract_discovered_attributes(
        html,
        discovery_result["found_attributes"],
        sku
    )
    return extraction_result


def discover_attributes(html: str, sku: str = "", taxonomy: str = None) -> Dict:
    taxonomy_context = ""
    if taxonomy:
        taxonomy_context = f"CONTEXT: This product belongs to the category: '{taxonomy}'. Prioritize finding attributes standard for this category."
    prompt = f"""
    You are analyzing an HTML product page to discover what technical specifications exist.
    {taxonomy_context}
    Your job: Identify ALL attribute names/labels that appear in the HTML, especially in:
    - Table headers or row labels
    - Definition list terms (<dt>)
    - Labels before colons (e.g., "Battery Capacity:", "Material:")
    - Section headings containing "specifications", "details"
    Do NOT extract values yet - only find the attribute NAMES.
    HTML (first 10000 chars):
    {html[:10000]}
    Output ONLY JSON:
    {{
    "found_attributes": ["attribute name 1", "attribute name 2", ...],
    "product_type_hint": "brief description of what this product appears to be"
    }}
    """
    schema = {
        "type": "object",
        "properties": {
            "found_attributes": {"type": "array", "items": {"type": "string"}},
            "product_type_hint": {"type": "string"}
        },
        "required": ["found_attributes"]
    }
    try:
        result = safe_call_llm(prompt, schema, "discover_attributes")
        return result
    except Exception as e:
        logger.error(f"Schema discovery failed for {sku}: {e}")
        return {"found_attributes": [], "error": str(e)}


def extract_discovered_attributes(html: str, attribute_names: list, sku: str = "") -> Dict:
    if not attribute_names:
        return {"source": "web", "attributes": {}, "error": "no_attributes_discovered"}
    prompt = f"""
You are extracting specific technical specifications from HTML.
Extract the VALUES for these attributes (if they exist in the HTML):
{', '.join(attribute_names[:50])}  
Rules:
- Extract EXACTLY as written (preserve units, formatting, capitalization)
- If an attribute appears multiple times, use the most detailed/complete value
- If an attribute is not found, omit it (don't include null values)
- Look in tables, lists, divs, and any structured data
- Return a FLAT JSON object.
- Example: {{ "Battery Capacity": "3,349 mAh" }}
- DO NOT DO THIS: {{ "Battery Capacity": {{ "Battery Capacity": "3,349 mAh" }} }}
- Ignore all technical metadata such as 'Ray ID', 'Cloudflare', 'Access Denied', or 'Cookies Consent'. Only extract product specifications.
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
        if not result or "attributes" not in result:
            logger.warning(f"Extraction failed for {sku}")
            return {"source": "web", "attributes": {}, "error": "extraction_failed"}
        attrs = result["attributes"]
        if not attrs or all(v is None or v == "" for v in attrs.values()):
            logger.warning(
                f"All extracted values are null/empty for {sku}, trying fallback")
            return {
                "source": "web",
                "attributes": fallback_extraction(html),
                "extraction_method": "fallback"
            }
        result["attributes"] = {
            k: v for k, v in attrs.items() if v is not None and v != ""}
        logger.info("Successfully extracted %d attributes for %s",
                    len(result['attributes']), sku)
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


def build_golden_record(
    standardized_data: Dict,
    identifiers: Dict,
    scraped_urls: List[str],
    taxonomy: Optional[str] = None,
    primary_attributes: Optional[List[str]] = None
) -> Dict:
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
    taxonomy_input_line = f"Category: {taxonomy}" if taxonomy else ""
    taxonomy_instruction_line = f"3. Include the taxonomy: {taxonomy}" if taxonomy else ""
    taxonomy_json_line = f'"taxonomy": "{taxonomy}",' if taxonomy else ""
    priority_instruction = ""
    if primary_attributes:
        attrs_list = "\n".join([f"- {a}" for a in primary_attributes])
        priority_instruction = f"""
USER-REQUESTED ATTRIBUTE NAMES (PRIORITY):
{attrs_list}
TASK: If any data in 'STANDARDIZED ATTRIBUTES' matches the meaning of a 'USER-REQUESTED' name above (e.g. 'color_temp' matches 'Color Temperature'), you MUST use the 'USER-REQUESTED' name as your JSON key.
"""
    attribute_names_list = ", ".join(
        [f'"{k}"' for k in standardized_data.keys()])
    feature_rules = f"""
 CRITICAL: FEATURES RULES:
- Features should be BENEFIT-ORIENTED descriptions, NOT raw specifications
- DO NOT repeat information already in attributes
- Attributes already extracted: {attribute_names_list}
- BAD Feature: "Color Temperature: 4000K" (this is in attributes)
- GOOD Feature: "4000K color temperature for clear, natural visibility"
- BAD Feature: "Material: Aluminum" (this is in attributes)
- GOOD Feature: "Durable aluminum construction withstands harsh conditions"
- Format: Convert specs into benefits/advantages/use cases
- Focus on: What makes this product valuable? Why would someone buy it?
GOOD FEATURE EXAMPLES:
- "High output of 18,000 lumens illuminates large spaces effectively"
- "Energy-efficient LED technology reduces utility costs by up to 60%"
- "Durable die-cast aluminum housing ensures long-lasting performance"
- "UL and DLC certified for quality assurance and energy rebates"
"""
    prompt = f"""
Create a product Golden Record and return the result as JSON.
INPUT DATA:
SKU/MPN: {identifiers.get('mpn')}
Brand: {identifiers.get('brand')}
{taxonomy_input_line}
STANDARDIZED ATTRIBUTES (Found Data):
{json.dumps(standardized_data, indent=2)}
{priority_instruction}
YOUR TASK:
Create a clean JSON object with:
1. Copy the SKU and brand from above
2. Include ALL standardized attributes
{taxonomy_instruction_line}
3. If specific attribute names were provided, use those EXACT names as keys
4. DATA SANITY CHECK: Ensure attributes belong to the product type
   - Example: If product is "Light Fixture", ignore "Screen Size" or "RAM"
   - Remove any data that clearly belongs to a different product
5. CONTENT GENERATION:
   - short_description: Write 1-2 sentences summarizing the product
   - long_description: Write 2-3 paragraphs of detailed marketing copy  
   - features: Generate 5-10 benefit-oriented bullet points (see rules below)
{feature_rules}
6. Set ready_for_publish based on: has brand ({has_brand}) AND at least 4 specs ({tech_spec_count} found)
7. Assign confidence 0.0-1.0 based on data completeness
Return ONLY this JSON structure (no markdown, no extra text):
{{
  "sku": "the SKU value",
  "brand": "the brand value",
  {taxonomy_json_line}
  "attributes": {{
    "attribute_name": "value"
  }},
  "short_description": "1-2 sentence summary",
  "long_description": "2-3 paragraph detailed description",
  "features": [
    "Benefit-oriented feature 1",
    "Benefit-oriented feature 2",
    ...5-10 features total
  ],
  "ready_for_publish": true or false,
  "confidence": 0.0 to 1.0
}}
CRITICAL: 
- Response must be valid JSON only
- Features must be benefits, NOT specs repetition
- Do not repeat attribute data in features
"""
    schema = {
        'type': 'object',
        'properties': {
            'sku': {'type': 'string'},
            'brand': {'type': 'string'},
            'taxonomy': {'type': 'string'},
            'attributes': {'type': 'object'},
            'short_description': {'type': 'string'},
            'long_description': {'type': 'string'},
            'features': {
                'type': 'array',
                'items': {'type': 'string'},
                'minItems': 5,
                'maxItems': 10
            },
            'ready_for_publish': {'type': 'boolean'},
            'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1}
        },
        'required': ['sku', 'brand', 'attributes', 'ready_for_publish'],
        'additionalProperties': False
    }
    try:
        result = safe_call_llm(prompt, schema, 'build_golden_record')
        if not result or 'error' in result:
            raise ValueError(
                f"LLM returned error: {result.get('error', 'unknown')}"
            )
        missing = [f for f in ['sku', 'brand', 'attributes', 'ready_for_publish']
                   if f not in result]
        if missing:
            raise ValueError(f"Missing required fields: {missing}")
        if not result.get('attributes'):
            raise ValueError("Empty attributes")
        if taxonomy and 'taxonomy' not in result:
            result['taxonomy'] = taxonomy
        logger.info(
            f"✓ Golden record for {result['sku']}: "
            f"{len(result['attributes'])} attrs, "
            f"ready={result['ready_for_publish']}"
        )
        result['sources_consulted'] = scraped_urls
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
        fallback_features = []
        for attr_name, attr_val in list(standardized_data.items())[:5]:
            if isinstance(attr_val, dict):
                val = attr_val.get('value', attr_val)
            else:
                val = attr_val
            fallback_features.append(f"Features {attr_name}: {val}")
        return {
            'sku': identifiers.get('mpn', 'UNKNOWN'),
            'brand': identifiers.get('brand', 'UNKNOWN'),
            'taxonomy': taxonomy,
            'attributes': standardized_data,
            'short_description': f"{identifiers.get('brand', 'Quality')} {identifiers.get('mpn', 'product')}",
            'long_description': f"Professional grade product from {identifiers.get('brand', 'leading manufacturer')}.",
            'features': fallback_features,
            'ready_for_publish': has_brand and tech_spec_count >= 4,
            'confidence': 0.7 if tech_spec_count >= 5 else 0.5,
            'sources_consulted': scraped_urls,
            'generated_by': 'deterministic_fallback',
            'reason': str(e)
        }
