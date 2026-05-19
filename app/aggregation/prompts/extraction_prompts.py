import logging
from typing import Optional,List
logger=logging.getLogger('extraction_prompts')
def build_extraction_prompt(product_name: str, mpn: str, brand: str, taxonomy: str, 
                           primary_attributes: list, html_content: str, 
                           candidate_images: Optional[List[str]] = None) -> dict:
    try:
        primary_list = primary_attributes if primary_attributes else []
        candidate_section = ""
        if candidate_images:
            candidate_section = "\nCANDIDATE IMAGES FROM IMAGE SEARCH:\n"
            for img_url in candidate_images[:5]:
                candidate_section += f"  - {img_url}\n"
        
        # NEW: Make distinction clear
        primary_attrs_display = "\n".join([f"  {i+1}. {attr}" for i, attr in enumerate(primary_list)])
        html_begin = html_content[:60000]  # First 60k chars (product info)
        html_end = html_content[-50000:] if len(html_content) > 110000 else ""  
        prompt = f"""
        You are extracting technical specifications from product content.

        PRODUCT CONTEXT:
        - Name: {product_name}
        - MPN: {mpn}
        - Brand: {brand}
        - Category: {taxonomy}

        ═══════════════════════════════════════════════════════════════════
CRITICAL RULE: NO HALLUCINATION - STRICT EXTRACTION ONLY
═══════════════════════════════════════════════════════════════════

- ONLY extract values that are EXPLICITLY VISIBLE in the provided HTML content
- Do NOT infer, guess, assume, or add any information from your training data
- If a specification is not present in the content, DO NOT create it
- Do NOT add "typical" values, default values, or common specifications
- Do NOT complete partial information
- Extract ONLY what you SEE, exactly as you SEE it

EXAMPLES OF HALLUCINATION (DO NOT DO):
 HTML shows "Maximum Working Pressure: 10000" → Extracting "Hose Length: 30 inches"
 HTML shows "Includes: Coupler" → Extracting "Hose Length: 24 inches"
 HTML shows no weight → Extracting "Weight: 5 lbs" (from training data)

CORRECT BEHAVIOR:
 HTML shows "Maximum Working Pressure: 10000" → Extract ONLY that
 HTML shows no weight → Do NOT extract any weight attribute
 If a specification table row is empty or blank → Do NOT extract it

REMEMBER: If you cannot see it in the HTML, it does not exist for this product.
Better to miss a specification than to invent a wrong one.

═══════════════════════════════════════════════════════════════════
EXTRACTION SCOPE - READ CAREFULLY
═══════════════════════════════════════════════════════════════════
        YOU MUST EXTRACT **ALL TECHNICAL SPECIFICATIONS** FROM THE CONTENT.

        PRIORITY ATTRIBUTES (must extract if present):
        {primary_attrs_display}

        ADDITIONAL ATTRIBUTES (extract ALL you find):
        - Extract EVERY other technical specification on the page
        - Maximum Working Pressure, Output Per Stroke, Pump Material, etc.
        - Material types, dimensions, performance specs, compatibility info
        - NO LIMIT on how many attributes to extract

        **DO NOT stop after finding priority attributes. Keep extracting ALL specs.**

        ═══════════════════════════════════════════════════════════════════
        CRITICAL INSTRUCTION: SEMANTIC ATTRIBUTE MAPPING
        ═══════════════════════════════════════════════════════════════════
        The HTML may use DIFFERENT words than the PRIMARY ATTRIBUTES list.
        Your job: understand the MEANING and map accordingly.
        MATCHING ALGORITHM:
        For each specification you find in the HTML:
        1️ IDENTIFY: What concept does this represent?
          Examples:
          - "Screen Size" → concept: size of display
          - "Processor Speed" → concept: CPU performance metric
          - "SPF Rating" → concept: sun protection level
          - "Thread Count" → concept: fabric density
        2️ SCAN: Does any PRIMARY ATTRIBUTE represent the same concept?
          Look for:
          - Exact match: "Screen Size" in primary list ✓
          - Synonym: "Display Size" in primary list ✓
          - Abbreviated: "CPU Speed" vs "Processor Speed" ✓
          - Generic→Specific: "Size" vs "Screen Size" ✓
          - Type→Material: "Adhesive Type" vs "Adhesive Material" ✓
        3️ DECIDE:
            MATCH FOUND → Use the PRIMARY ATTRIBUTE name
          NO MATCH → Use the name from HTML (still extract it!)
        LINGUISTIC PATTERNS TO RECOGNIZE:
        Pattern A: "X Type" often means "X Material" or just "X"
          "Adhesive Type" → "Adhesive Material" or "Adhesive"
          "Fabric Type" → "Fabric Material" or "Fabric"
          "Skin Type" → "Skin Type" (keep as is if specific)
        Pattern B: "X Rating" / "Rated X" / "X Spec" means just "X"
          "Voltage Rating" → "Voltage"
          "SPF Rating" → "SPF"
          "Thread Count Spec" → "Thread Count"
        Pattern C: "Overall X" / "Total X" / "Nominal X" means "X"
          "Overall Length" → "Length"
          "Total Capacity" → "Capacity"
          "Nominal Diameter" → "Diameter"
        Pattern D: Abbreviations expand to full terms
          "CCT" → "Color Temperature"
          "OD" → "Outer Diameter"
          "mAh" → matches "Battery Capacity" (if capacity measured in mAh)
          "RAM" → "Memory"
        Pattern E: Technical synonyms (context-dependent)
          Electronics: "Cell Chemistry" = "Battery Type"
          Textiles: "Thread Count" = "Fabric Density"
          Cosmetics: "Active Ingredient" = "Formula"
          Food: "Net Weight" = "Weight"
        ═══════════════════════════════════════════════════════════════════
        EXAMPLES OF SEMANTIC MATCHING (ACROSS ANY CATEGORY)
        ═══════════════════════════════════════════════════════════════════
        Scenario 1: EXACT MATCH
          HTML: "Screen Size: 6.1 inches"
          Primary List: ["Screen Size", "Battery Capacity", "Camera Resolution"]
          → Extract as: name="Screen Size", value="6.1 inches"
        Scenario 2: SYNONYM MATCH
          HTML: "Display Size: 6.1 inches"
          Primary List: ["Screen Size", "Battery Capacity"]
          → Extract as: name="Screen Size", value="6.1 inches"
          (Reason: "Display Size" = "Screen Size")
        Scenario 3: PATTERN MATCH (Rating)
          HTML: "SPF Rating: 50"
          Primary List: ["SPF", "Water Resistance"]
          → Extract as: name="SPF", value="50"
          (Reason: "SPF Rating" → "SPF")
        Scenario 4: PATTERN MATCH (Type→Material)
          HTML: "Fabric Type: Cotton"
          Primary List: ["Fabric Material", "Thread Count"]
          → Extract as: name="Fabric Material", value="Cotton"
          (Reason: "Fabric Type" → "Fabric Material")
        Scenario 5: NO MATCH (Still extract!)
          HTML: "Fragrance: Lavender"
          Primary List: ["Volume", "SPF"]
          → Extract as: name="Fragrance", value="Lavender"
          (Reason: No match in primary list, but still relevant)
        Scenario 6: ABBREVIATION MATCH
          HTML: "RAM: 8GB"
          Primary List: ["Memory", "Storage"]
          → Extract as: name="Memory", value="8GB"
          (Reason: "RAM" is abbreviation for "Memory")
          ═══════════════════════════════════════════════════════════════════
        EXTRACTION RULES
        ═══════════════════════════════════════════════════════════════════
        ✓ DO EXTRACT:
          - ALL technical specifications with values (NO MAXIMUM)
          - Priority attributes from list above
          - Every other spec you find: dimensions, materials, ratings, capacities
          - Measurements with units (always include units!)
          - Material/composition information
          - Performance ratings
          - Compatibility information
          - Certifications and standards
          
        **IMPORTANT: Extract 20-30+ attributes if page has them. Do not limit yourself.**

        ✗ DO NOT EXTRACT:
          - Marketing claims ("best in class", "premium quality")
          - Website metadata (dates, IDs, page numbers)
          - Product category (already in context)
          - Pricing, availability, shipping info
          - Internal SKUs/codes (unless in PRIMARY ATTRIBUTES)
          - Customer reviews or ratings
        VALUE RULES:
          - Only extract values you SEE in the content
          - Do NOT calculate, estimate, or infer
          - Always include units: "100ml" not "100"
          - If range given, extract the range: "10-15 kg" not "12.5 kg"
        ADDITIONAL ATTRIBUTES:
          - Extract ALL relevant specs beyond PRIMARY ATTRIBUTES (no maximum)
          - Use the exact names from HTML for these
          - Prioritize technical/measurable attributes
        PRODUCT VERIFICATION:
          - Verify content is about "{mpn}"
          - If dominated by OTHER product codes → set "product_detected": false
        ═══════════════════════════════════════════════════════════════════
        CONTENT TO ANALYZE (BEGINNING - product overview and features):
        ═══════════════════════════════════════════════════════════════════
        {html_begin}

        ═══════════════════════════════════════════════════════════════════
        CONTENT TO ANALYZE (END - often contains specification tables):
        ═══════════════════════════════════════════════════════════════════
        {html_end if html_end else ""}
        {candidate_section}
        ═══════════════════════════════════════════════════════════════════
        IMAGE EXTRACTION - CRITICAL RULES
        ═══════════════════════════════════════════════════════════════════
        Find the main product image URL. Follow these rules STRICTLY:
        1. PRIORITY ORDER (check in this sequence):
          a) <meta property="og:image" content="...">
          b) <meta name="twitter:image" content="...">
          c) <img> tags with attributes: data-zoom, data-image, data-src
          d) <img> tags with class/id containing: product, main, hero, primary
          e) <img> tags with src containing the MPN: "{mpn}"
        2. URL VALIDATION:
            MUST be absolute URL starting with http:// or https://
            MUST end with image extension: .jpg, .jpeg, .png, .webp, .gif
            IGNORE: icons, logos, thumbnails, badges, social-media images
            IGNORE: SVG files, data URIs, placeholder images
        3. URL COMPLETION:
          - If you find a URL WITHOUT extension, look for the same base URL 
            with common extensions in nearby attributes
          - Example: If src="image123", check data-zoom="image123.jpg"
        4. SIZE PREFERENCE:
          - Prefer larger images: look for "large", "zoom", "1200", "hires"
          - Avoid thumbnails: skip "thumb", "small", "100x100"
        5. OUTPUT REQUIREMENTS:
          - Return COMPLETE URL including extension
          - If no valid image found → return null
          - Do NOT return incomplete URLs
          - Do NOT return placeholder URLs
        EXAMPLES:
        BAD OUTPUT:
        "https://assets.dewalt.com/.../product_image"  (missing extension)
        GOOD OUTPUT:
        "https://assets.dewalt.com/.../product_image.jpg"
        BAD OUTPUT:
        "https://example.com/logo.svg"  (logo, not product)
        GOOD OUTPUT:
        "https://example.com/products/dcf414b-main-1200x1200.jpg"
        ═══════════════════════════════════════════════════════════════════
        ═══════════════════════════════════════════════════════════════════
BEFORE RETURNING, VERIFY:
═══════════════════════════════════════════════════════════════════
- Did I extract any value that was NOT visible in the HTML? → If yes, REMOVE it
- Did I assume or guess any specification? → If yes, REMOVE it
- Did I add units that weren't specified? → If yes, correct or remove
- Did I complete a partial value? → If yes, use only what was actually written

If you are unsure about any extracted value, DO NOT include it.
Empty values are better than wrong values.

═══════════════════════════════════════════════════════════════════
OUTPUT
        ═══════════════════════════════════════════════════════════════════
        OUTPUT
        ═══════════════════════════════════════════════════════════════════
        Return JSON following ExtractionResponse schema:
        - attributes: list of extracted specifications
        - product_detected: true/false
        - product_type: category if detected
        - image_url: product image URL or null
        """
        return {
            'prompt': prompt,
            'response_schema': "ExtractionResponse",
            'max_tokens': 8000
        }
    except Exception as e:
        logger.error(f"Build_extraction_prompt failed: {e}")
        return None
def build_pdf_extraction_prompt(product_name:str,mpn:str,brand:str,taxonomy: str,pdf_text:str,primary_attributes:list)->dict:
    try:
        primary_list = primary_attributes if primary_attributes else []  
        prompt=f"""
        You are extracting specifications from an official product datasheet.
        PRODUCT:
        -MPN :{mpn}
        -Brand :{brand}
        -Name:{product_name}
        CRITICAL ATTRIBUTES (required):
        {chr(10).join([f"  {i+1}. {attr}" for i, attr in enumerate(primary_list)])}
        DATASHEET_CONTENT:
        {pdf_text[:10000]}
        EXTRACTION_RULES:
        1. PDFs are official sources - extract everything you find
        2. Look for specification tables, technical sections
        3. Extract values with units exactly as shown
        4. If a spec has multiple values (min/max), extract the typical/nominal
        5. Extract ALL additional specifications you find (no maximum)
        IGNORE:
        - Copyright notices
        - Company addresses
        - Ordering information
        - Marketing sections
        Return JSON following ExtractionResponse schema.
        IMAGE EXTRACTION:
- Some PDFs contain embedded images or reference image URLs in text.
- If you find a URL that clearly points to a product image (e.g., ends with .jpg/.png and contains product keywords), extract it.
- Otherwise, set `image_url` to `null`.
        Confidence  should be 0.95+ for PDF sources
        """
        return{
            'prompt':prompt,
            'response_schema':'ExtractionResponse',
            'max_tokens':8000,
            'source_type':'pdf'
        }
    except Exception as e:
        logger.error(f"pdf extraction prompt  failed str{e}")
        return