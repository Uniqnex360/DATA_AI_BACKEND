
import logging
from typing import Dict, List, Optional
from app.llm import call_llm_with_schema,_llm_semaphore

logger = logging.getLogger("standardization")
class BatchStandardizer:
    
    def __init__(self, target_market: str = 'US'):
        self.target_market = target_market
    async def standardize_attributes(self, attributes: List[Dict]) -> List[Dict]:
        if not attributes:
            return []
        prompt = self._build_batch_prompt(attributes)
        async with _llm_semaphore:
            result = await call_llm_with_schema(
                prompt=prompt,
                response_model="StandardizationResponse",
                estimated_tokens=1000
            )
        standardized = []
        for i, attr in enumerate(attributes):
            if i < len(result.standardized_attributes):
                std_attr = result.standardized_attributes[i]
                standardized.append({
                    **attr,
                    'value': std_attr.value,
                    'unit': std_attr.unit,
                    'original_value': attr['value'],
                    'original_unit': attr.get('unit'),
                    'conversion_applied': std_attr.conversion_applied
                })
            else:
                standardized.append(attr)
        logger.info(f" Batch standardized {len(standardized)} attributes in 1 LLM call")
        return standardized
    def _build_batch_prompt(self, attributes: List[Dict]) -> str:
        attrs_text = ""
        for i, attr in enumerate(attributes, 1):
            attrs_text += f"{i}. {attr['name']}: {attr['value']} {attr.get('unit', '')}\n"
        prompt = f"""You are a product data standardization engine.
TARGET MARKET: {self.target_market}
{'Prefer imperial units (inches, pounds, PSI, °F)' if self.target_market == 'US' else 'Prefer metric units (cm, kg, bar, °C)'}
ATTRIBUTES TO STANDARDIZE:
{attrs_text}
STANDARDIZATION RULES:
1. NUMERIC VALUES:
   - Remove commas: "1,000" → "1000"
   - Normalize decimals: "10.00" → "10"
   - Remove trailing zeros: "10.50" → "10.5"
   - Convert ranges to average: "10-20" → "15"
2. UNIT CONVERSIONS (for {self.target_market} market):
   {'- Convert all to: inches, pounds, PSI, °F' if self.target_market == 'US' else '- Convert all to: cm, kg, bar, °C'}
   - Examples:
     * "24 inches" → value: "24", unit: "in"
     * "10.5 kg" → value: "23.15", unit: "lb" (US) OR keep "10.5", "kg" (EU)
     * "100-150 PSI" → value: "125", unit: "psi"
3. TEXT VALUES:
   - Trim whitespace
   - Standardize booleans: yes/y/true/1 → "Yes", no/n/false/0 → "No"
   - Proper capitalization
   - Remove "N/A", "unknown", "-"
4. CONSISTENCY ACROSS ATTRIBUTES:
   - Use same unit system for all dimensions (all inches OR all cm)
   - If one attribute is "Length: 24 in", then "Width: 12 in" (not cm)
5. PRESERVE ORIGINALS:
   - Always keep track of what you changed
   - Note: "conversion_applied" explains the transformation
Return a StandardizationResponse with standardized_attributes matching the input order.
Each attribute must have: name, value, unit, conversion_applied.
CRITICAL: Maintain the EXACT ORDER of attributes as provided above.
"""
        return prompt
async def standardize_with_llm(attribute: str, values: List[str]) -> dict:
    """
    Legacy function signature - now uses batch standardizer internally
     DEPRECATED: Use BatchStandardizer directly for better performance
    """
    logger.warning(
        "  Using deprecated standardize_with_llm(). "
        "Switch to BatchStandardizer for 10x better performance."
    )
    if not values:
        return {"standard_value": None, "unit": None, "derived_from": []}
    fake_attrs = [{
        'name': attribute,
        'value': values[0] if values else '',
        'unit': None
    }]
    standardizer = BatchStandardizer(target_market='US')
    result = await standardizer.standardize_attributes(fake_attrs)
    if result:
        return {
            'standard_value': result[0]['value'],
            'unit': result[0].get('unit'),
            'derived_from': values
        }
    return {"standard_value": None, "unit": None, "derived_from": values}