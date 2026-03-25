import asyncio
import logging
from re import S
from typing import List, Optional
from pydantic import BaseModel, Field
from app import llm
from app.core.rate_limiter import openai_limiter
logger = logging.getLogger('cleaning_service')
class AttributeInput(BaseModel):
    id: str                     
    name: str
    value: str
    unit: Optional[str] = None
    source: Optional[str] = None   
class ProductContext(BaseModel):
    mpn: Optional[str] = None
    brand: Optional[str] = None
    product_name: Optional[str] = None
    taxonomy: Optional[str] = None
class CleanedAttribute(BaseModel):
    id: str
    name: str
    original_value: str
    cleaned_value: str
    unit: Optional[str] = None
    cleaning_reason: str            
    issue_detected: bool = False    
class LLMCleaningResponse(BaseModel):
    cleaned_attributes: List[CleanedAttribute]
    summary: str = Field(description="Brief summary of cleaning actions taken")
class LLMCleaningService:
    def __init__(self,llm_provider:str, model: str = "gpt-4o-mini", max_retries: int = 3, concurrency_limit: int = 10):
        self.llm_provider=llm_provider
        self.model = model
        self.max_retries = max_retries
        self._semaphore = asyncio.Semaphore(concurrency_limit)  
    async def clean_attributes(self,attributes: List[AttributeInput],context: ProductContext) -> LLMCleaningResponse:        
        from app.aggregation.aggregate_product import call_llm_with_schema
        logger.info(f"Starting cleaning for {len(attributes)} attributes")
        if not attributes:
            return LLMCleaningResponse(
                cleaned_attributes=[],
                summary="No attributes to clean"
            )
        async with self._semaphore:
            prompt = self._build_prompt(attributes, context)
            for attempt in range(self.max_retries):
                try:
                    estimated_tokens = 500 + len(attributes) * 200
                    await openai_limiter.wait_if_needed(estimated_tokens=estimated_tokens)
                    response = await call_llm_with_schema(
                        prompt=prompt,
                        response_model="LLMCleaningResponse",
                        llm_provider=self.llm_provider,
                        model=self.model,
                        estimated_tokens=estimated_tokens,
                        max_tokens=2000
                    )
                    logger.info(f"LLM response received, cleaned {len(response.cleaned_attributes)} attributes")
                    input_ids = {a.id for a in attributes}
                    response_ids = {ca.id for ca in response.cleaned_attributes}
                    missing_ids = input_ids - response_ids
                    if missing_ids:
                        logger.warning(f"LLM missing attributes: {missing_ids}. Adding unchanged.")
                        for attr in attributes:
                            if attr.id in missing_ids:
                                response.cleaned_attributes.append(CleanedAttribute(
                                    id=attr.id,
                                    name=attr.name,
                                    original_value=attr.value,
                                    cleaned_value=attr.value,
                                    unit=attr.unit,
                                    cleaning_reason="No change (LLM omitted)",
                                    issue_detected=False
                                ))
                    return response
                except Exception as e:
                    logger.error(f"Cleaning attempt {attempt+1} failed: {e}")
                    if attempt == self.max_retries - 1:
                        return self._fallback_response(attributes, f"LLM error after {self.max_retries} attempts: {e}")
                    await asyncio.sleep(2 ** attempt)  
        return self._fallback_response(attributes, "Max retries exceeded")
    def _build_prompt(self, attributes: List[AttributeInput], context: ProductContext) -> str:
        attr_lines = []
        for attr in attributes:
            line = f"ID: {attr.id}\n  Name: {attr.name}\n  Value: {attr.value}"
            if attr.unit:
                line += f"\n  Unit: {attr.unit}"
            if attr.source:
                line += f"\n  Source: {attr.source}"
            attr_lines.append(line)
        attributes_text = "\n\n".join(attr_lines)
        rules_text = """
       TASK:
1. **STRICT UNIT SEPARATION**: Move ALL technical units (VDC, W, Mbps, in, mm, deg F, deg C, etc.) to the 'unit' field. This includes units inside ranges. 
   - Value "37 to 55 VDC" MUST become cleaned_value: "37 to 55", unit: "VDC".
2. **CLEAN LABELS**: Format dimensions exactly as "Value L x Value W x Value H".
3. **CASE FIXING**: For statuses like "TEMPORARY / PERMANENT", output only the active status in Title Case (e.g., "Permanent").
4. **NO UNITS IN VALUE**: If you populate the 'unit' field, the 'cleaned_value' MUST NOT contain that unit string.

5. **CROSS-REFERENCE UNIFICATION**: Review all attributes for this product. If "Backing Material" and "Material" both refer to the same substance, ensure they use the EXACT same canonical string (e.g., both must be "Polyvinyl Chloride").
6. **MATERIAL EXPANSION**: Convert all shorthand (PVC, SS) to full industry names across the entire attribute set.
- **Attribute Name Casing**: Every attribute name (e.g., the key in your response) MUST be in Title Case or Sentence Case. 
  * NEVER use all caps (e.g., "MPN" is okay, but "COLOR" must be "Color").
  * Example: "TEMPORARY / PERMANENT" -> "Temporary / Permanent".
- Mathematical Rounding: Round decimals to 2 places (e.g., 1.998 -> 2.00).  
- Phrase Deduplication: Remove redundant or repeating words/phrases within a single value.
  * Example: "Double-Sided Tape Double Sided Tape" -> "Double Sided Tape".
  * Example: "Black Black" -> "Black".
- Punctuation Normalization: Prefer spaces over hyphens for descriptive text unless it's a technical standard.
  * Example: "Double-Sided" -> "Double Sided".
- Semantic Merging: If the same attribute appears twice with slightly different formatting, merge them into one clean canonical value.
- **Semantic Normalization (Logic-Based)**: 
  * Analyze each value for technical or material synonyms. 
  * If a value is a known abbreviation or variant of a standard material or technical term (regardless of the specific example), you MUST convert it to its most widely recognized, full canonical name.
  * Resolve all chemical, material, and electronic shorthand to their industry-standard full terms.
  - **Semantic Normalization (Logic-Based)**: 
  * Analyze each value for technical or material synonyms. 
  * If a value is a known abbreviation or variant of a standard material or technical term (regardless of the specific example), you MUST convert it to its most widely recognized, full canonical name.
  * Resolve all chemical, material, and electronic shorthand to their industry-standard full terms.
- **Cross-Attribute Value Unification**: Values for similar concepts must be identical across different attribute names.
  * If 'Backing Material' is "Polyvinyl Chloride", then 'Material' MUST also be "Polyvinyl Chloride" (unify PVC to the full name).
  * If 'Adhesive Material' is "Rubber", ensure the value matches the material naming standard used elsewhere.

- **Deterministic Case Unification**: 
  * Ignore the input casing (UPPER, lower, or mIxed). 
  * All descriptive and categorical text must be output in Title Case. 
  * Treat "Word", "WORD", and "word" as the exact same semantic entity and unify them.

- **Discrete List Integrity**: 
  * Detect if a string represents a set of distinct options/speeds versus a continuous range. 
  * If the values are separated by commas or semicolons, you MUST preserve every unique number in a pipe-delimited list. 
  * NEVER summarize or calculate a range (e.g., 'min to max') for discrete sets.
- **Casing Standard**: Never use ALL CAPS or all lowercase. Always use Title Case or Sentence Case where the first letter is capitalized (e.g., "HEAVY DUTY" -> "Heavy Duty").
- Fraction to Decimal: For Length, Width, Height, Size → convert fractions (1-1/2, 3/4) to decimals (1.5, 0.75)
- Range Normalization: For Rating, Output, Input, Capacity → standardize hyphen spacing (10V - 12V → 10-12 V)
- Leading Zero Management: For UPC, GTIN, Part Numbers, Code → pad/strip zeros to required length (default 12 digits for UPC)
- Master UOM Expansion: For Length, Width, Size → expand symbols (1", 50m.m., 3 YD → 1 in., 50 mm, 3 yd.)
- Technical UOM Expansion: For Capacity, Output, Input, Speed, Resistance, Density → expand technical units (500rpm → 500 RPM, 12v → 12 V, 1000mah → 1000 mAh)
- Value/Unit Spacing: For all measurement attributes → enforce space between value and unit (4000K → 4000 K, 50W → 50 W)
Delimiter Standardization: For lists, use pipe | without repeating the unit in the value if the UOM field covers it.
- Boolean Standardization: For Feature, Variant → map 1/Y/TRUE to "Yes", others to "No"
- Base Mapping (Color/Material): For Finish, Material, Surface → map complex marketing names to base (Oil Rubbed Bronze → Bronze)
- Synonym Consolidation: For Material, Composition, Finish → merge variations (Alum., Blk, Nickle → Aluminum, Black, Nickel)
- Redundancy Removal: Strip attribute name from value (Material: Brass → Brass)
- Null / Empty Standardization: Convert N/A, None, -, TBD → blank (empty string)
- Whitespace Trimming: Remove leading/trailing/double spaces
- Case Standardization: Use Title Case for most text (HEAVY DUTY → Heavy Duty)
- Special Character Cleanup: Standardize hyphens, slashes (Chrome-Plated → Chrome Plated)
- Non-ASCII / Unicode Scrubbing: Remove corrupted characters, smart quotes (HeavyDuty / “Black” → Heavy Duty / "Black")
- HTML & Markdown Stripping: Remove tags from descriptions (<b>Waterproof</b> → Waterproof)
- Dimension Formatting: Always use the format "Value L x Value W x Value H".
  * Example: "4.15 x 2.11 x 1.33" -> "4.15 L x 2.11 W x 1.33 H".
  * Ensure the unit (in, mm) is moved to the `unit` field and NOT kept in the value.
- Unit Isolation (STRICT): The 'cleaned_value' must NOT contain the unit if the 'unit' field is populated. 
  * Example: Value: "100 m", Unit: "m" -> Value: "100", Unit: "m".
  * Example: Value: "57 VDC", Unit: "VDC" -> Value: "57", Unit: "VDC".
- Range Standardisation: Use "to" for ranges and ensure units are repeated for clarity.
  * Example: "-40 to +158 F" -> "-40 deg F to 158 deg F".
  * Example: "10V - 12V" -> "10 V to 12 V".
- Range Unit Extraction: For ranges, the unit MUST be moved to the 'unit' field.
  * Example: "-4 deg F to 140 deg F" -> cleaned_value: "-4 to 140", unit: "deg F".
  * Example: "37 VDC to 55 VDC" -> cleaned_value: "37 to 55", unit: "VDC".
- Status/Boolean Cleaning: For values representing status or categories (like Temporary/Permanent), convert to a single Title Case word.
  * Example: "TEMPORARY / PERMANENT" where the context is "Temporary" -> "Temporary".
  * Example: "Y" or "1" for a feature -> "Yes".
- Global Formatting: The 'cleaned_value' should NEVER contain the unit if the 'unit' field is present.
- Temperature Normalization: Always use "deg C" or "deg F".
  * Example: "-10 Degrees Celsius" -> "-10 deg C".
  * Example: "105 °C" -> "105 deg C".
  - Deduplication: If a value contains redundant information or repeats the same spec, merge them.
  * Example: "-10 deg C, -10 deg C" -> "-10 deg C".
- Regex Value Extraction: Extract specific codes (IP65, ISO 9001) from messy strings.
- Regex Text Extraction: Extract key descriptive terms from long text (e.g., "low-profile tread" → "Low Profile" for Profile/Design).
- Value Extraction from Mixed Strings: Extract numeric values with units (e.g., from "LED Bulb 4000K 50W" extract "4000 K" for Color Temperature, "50 W" for Wattage).
- Temperature Extraction: For attributes like Operating Temperature, extract min/max values (e.g., "-20 C" → "-20 deg C" as Minimum, "85 C" as Maximum).
- Auto-Concatenation: Combine attributes to build optimized SEO product titles (e.g., Brass + 50cc + V-Twin → Brass 50cc V-Twin Engine).
- Preserve original value for reference.
- If a value is already correct, leave unchanged and note "already standard".
- Flag any placeholder values (N/A, TBD) as issue_detected = true and set cleaned_value = "".
- **List vs. Range Detection**: 
  * If a value contains multiple distinct numbers (e.g., "10, 100, 1000"), it is a **LIST**, not a range. Use the pipe delimiter.
  * Correct: "10 Mbps, 100 Mbps, 1000 Mbps" -> cleaned_value: "10 | 100 | 1000", unit: "Mbps".
  * Incorrect: "10 to 1000".
  * Use "to" ONLY for continuous ranges (e.g., "-4 to +140").
- **STRICT Context-Aware Material Mapping**:
  * For the attribute "Material": You MUST use the industry abbreviation.
    - If the substance is Polyvinyl Chloride, value MUST be "PVC".
    - If the substance is Stainless Steel, value MUST be "Stainless Steel".
  * For the attributes "Backing Material" or "Adhesive Material": You MUST use the full canonical name.
    - Use "Polyvinyl Chloride" (NOT PVC).
    - Use "Rubber" (NOT Rubber Material).
  * For the attribute "Category":
    - Use "Insulating Tapes" (NOT Electrical Insulating Tapes).

- **STRICT Synonym Resolution (Use ONLY these terms)**:
  * Group [PVC, pvc, pvcs, Poly Chloride, Polyvinyl Chlorides] -> Map to "PVC" if Name is Material, else "Polyvinyl Chloride".
  * Group [SS, ss, Stainlesssteel] -> Map to "Stainless Steel".
  * Group [Rubber Material] -> Map to "Rubber".
  * Group [Electrical Insulating Tapes] -> Map to "Insulating Tapes".
- **Project-Wide Case Consistency**: 
  * All descriptive values (Temporary, Permanent, Black, Steel) MUST use Title Case. 
  * Never leave values in ALL CAPS like "PERMANENT" if another row uses "Permanent".

- **Unit Isolation Perfection**: 
  * Ensure the `unit` field is populated for EVERY attribute that has a measurement. If "Dimension" has `in`, then "Dimensions" must also have `in`.
"""

        prompt = f"""
You are a Senior Product Data Engineer. Standardize the provided attributes using the following logic.

--- MASTER RULES ---
{rules_text}

--- FINAL TASK PRIORITIES ---
1. **STRICT UNIT ISOLATION**: Move ALL technical units (VDC, W, Mbps, in, mm, deg F, etc.) to the 'unit' field. The 'cleaned_value' must contain ONLY numbers, dimension labels (L, W, H), and connectors (to, |, x).
2. **NO SUMMARIZATION**: Do not turn lists into ranges. "10, 100, 1000" MUST stay "10 | 100 | 1000".
3. **DIMENSION LABELLING**: Always format as "Value L x Value W x Value H".
4. **CASE UNIFICATION**: Force every descriptive string into Title Case. "BLACK" -> "Black".
5. **DEDUPLICATION**: Remove repeating phrases ("Double Sided Double Sided") and ensure synonymous attribute names have identical outputs.
6. **ATTRIBUTE NAME STANDARDIZATION**: You must output Attribute Names in Title Case. For example, change "TEMPORARY / PERMANENT" to "Temporary / Permanent".

INPUT ATTRIBUTES:
{attributes_text}

Return a JSON object following the LLMCleaningResponse schema.
"""
        return prompt

    def _fallback_response(self, attributes: List[AttributeInput], reason: str) -> LLMCleaningResponse:
        cleaned = []
        for attr in attributes:
            cleaned.append(CleanedAttribute(
                id=attr.id,
                name=attr.name,
                original_value=attr.value,
                cleaned_value=attr.value,
                unit=attr.unit,
                cleaning_reason=f"Fallback: {reason}",
                issue_detected=False
            ))
        return LLMCleaningResponse(
            cleaned_attributes=cleaned,
            summary=f"LLM unavailable, used original values. Reason: {reason}"
        )
