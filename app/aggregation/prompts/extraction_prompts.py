import logging
from typing import Optional,List
logger=logging.getLogger('extraction_prompts')
# def build_extraction_prompt(product_name:str,mpn:str,brand:str,taxonomy:str,primary_attributes: list,html_content:str,candidate_images: Optional[List[str]] = None)-> dict:
#     try:
#         primary_list=primary_attributes[:20]
#         candidate_section = ""
#         if candidate_images:
#             candidate_section = "\nCANDIDATE IMAGES FROM IMAGE SEARCH (check if any appear on this page):\n"
#             for img_url in candidate_images[:5]:
#                 candidate_section += f"  - {img_url}\n"
#             candidate_section += (
#                 "If you see one of these images in the HTML and it appears to be the main product image, "
#                 "return its URL. If not, still look for any product image as described below.\n"
#             )
#         prompt=f"""
#         You are a  technical specification extraction engine.
#         PRODUCT_CONTEXT
#         - Name : {product_name}
#         - MPN  :{mpn}
#         - Brand :{brand}
#         - Category :{taxonomy}
#         TASK: Extract technical specifications for the provided content.
#         PRIMARY ATTRIBUTES(extract these first)
#         {chr(10).join([f"  • {attr}" for attr in primary_list])}
#         EXTRACTION_RULES:
#         1.Only extract  if you see the exact value in the content
#         2.Do NOT invent or estimate values
#         3.Do NOT extract:
#           - Website metadata (dates, IDs, tracking codes)
#           - Marketing fluff("world's best","premium quality")
#           - Internal codes (unless specifically requested)
#           - **Category information** (e.g., fields named "Category", "Product Group") – these are already provided in the product context.
#         4.Extract upto 10 ADDITIONAL relevant  technical specs beyond the primary list 
#         5. Use exact values with units (e.g., "1.5 kg" not "1.5")
#         PRODUCT VERIFICATION:
#         - First, verify the content is actually about "{mpn}"
#         - If you find references to OTHER product codes, set "product_detected": false
        
#         CONTENT TO ANALYZE:
#         {html_content[:20000]}
#     {candidate_section}
#     IMAGE EXTRACTION (CRITICAL):
#     - Locate the main product image URL in the HTML. Look for:
#     * `<meta property="og:image" content="...">` (Open Graph)
#     * `<meta name="twitter:image" content="...">` (Twitter Card)
#     * `<img>` tags with `src` containing keywords like "product", "main", "hero", "full", or the MPN `{mpn}`
#     * `<link rel="image_src" href="...">`
#     - Ignore small icons, logos, or thumbnails.
#     **SOURCE ATTRIBUTE**: Look for the `src` attribute inside `<img>` tags.
# #    **IGNORE HIDDEN IMAGES**: **DO NOT** select images that have `style="display: none"` or `visibility: hidden`.
# #      **IGNORE LOGOS**: Do not select images containing "logo", "icon", "nav", "header", "footer", or "social" in the URL.
# #       **IGNORE ID NOIMG**: Do not select images where `id="noimg"`.
# #         **META TAGS**: Check `<meta property="og:image" content="...">` first. If found and valid, use it.
# #         **RELATIVE URLS**: If the image src is relative (e.g., "/images/items/6602.jpg"), construct the absolute URL using the Source URL.
# #        **DO NOT GUESS**: Do not construct a URL if it does not exist in the HTML.
#     - Return the absolute URL (if relative, assume it's relative to the page domain).
#     - If no product image found, set `image_url` to `null`.
                
#     Return JSON following the ExtractionResponse schema with:
#     - attributes: list of extracted specifications
#     - product_detected: true/false
#     - product_type: category if detected
#     - image_url: product image URL or null

#         """
#         return {
#             'prompt':prompt,
#             'response_schema':"ExtractionResponse",
#             'max_tokens':2000
#         }
#     except Exception as e:
#         logger.error(f"Build_extraction_prompt failed str{e}")
#         return
def build_extraction_prompt(product_name: str, mpn: str, brand: str, taxonomy: str, 
                           primary_attributes: list, html_content: str, 
                           candidate_images: Optional[List[str]] = None) -> dict:
    try:
        primary_list = primary_attributes[:20]
        candidate_section = ""
        if candidate_images:
            candidate_section = "\nCANDIDATE IMAGES FROM IMAGE SEARCH:\n"
            for img_url in candidate_images[:5]:
                candidate_section += f"  - {img_url}\n"

        # Build primary attributes display
        primary_attrs_display = "\n".join([f"  {i+1}. {attr}" for i, attr in enumerate(primary_list)])

        prompt = f"""
You are extracting technical specifications from product content.

PRODUCT CONTEXT:
- Name: {product_name}
- MPN: {mpn}
- Brand: {brand}
- Category: {taxonomy}

PRIMARY ATTRIBUTES (extract these first):
{primary_attrs_display}

═══════════════════════════════════════════════════════════════════
CRITICAL INSTRUCTION: SEMANTIC ATTRIBUTE MAPPING
═══════════════════════════════════════════════════════════════════

The HTML may use DIFFERENT words than the PRIMARY ATTRIBUTES list.
Your job: understand the MEANING and map accordingly.

MATCHING ALGORITHM:

For each specification you find in the HTML:

1️⃣ IDENTIFY: What concept does this represent?
   Examples:
   - "Screen Size" → concept: size of display
   - "Processor Speed" → concept: CPU performance metric
   - "SPF Rating" → concept: sun protection level
   - "Thread Count" → concept: fabric density

2️⃣ SCAN: Does any PRIMARY ATTRIBUTE represent the same concept?
   Look for:
   - Exact match: "Screen Size" in primary list ✓
   - Synonym: "Display Size" in primary list ✓
   - Abbreviated: "CPU Speed" vs "Processor Speed" ✓
   - Generic→Specific: "Size" vs "Screen Size" ✓
   - Type→Material: "Adhesive Type" vs "Adhesive Material" ✓

3️⃣ DECIDE:
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
  - Technical specifications with values
  - Measurements with units (always include units!)
  - Material/composition information
  - Performance ratings
  - Compatibility information
  - Certifications and standards

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
  - Extract up to 10 relevant specs beyond PRIMARY ATTRIBUTES
  - Use the exact names from HTML for these
  - Prioritize technical/measurable attributes

PRODUCT VERIFICATION:
  - Verify content is about "{mpn}"
  - If dominated by OTHER product codes → set "product_detected": false

═══════════════════════════════════════════════════════════════════
CONTENT TO ANALYZE
═══════════════════════════════════════════════════════════════════
{html_content[:20000]}

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
            'max_tokens': 2000
        }
    except Exception as e:
        logger.error(f"Build_extraction_prompt failed: {e}")
        return None

# def build_extraction_prompt(product_name: str, mpn: str, brand: str, taxonomy: str, primary_attributes: list, html_content: str, url: str = "") -> dict:
#     try:
#         # Increase slice slightly to ensure we hit the body content, 
#         # but keep it within LLM limits (12k-15k is usually safe for GPT-4o-mini/Flash)
#         content_slice = html_content[:15000] 
#         primary_list = primary_attributes[:30] # Increased limit for better coverage
        
#         prompt = f"""
#         You are a highly precise technical specification extraction engine.
        
#         PRODUCT_CONTEXT
#         - Target MPN: {mpn}
#         - Target Brand: {brand}
#         - Target Name: {product_name}
#         - Target Category: {taxonomy}
#         - Page URL: {url}

#         TASK:
#         1. Verify if this page contains specifications for the Target MPN: "{mpn}".
#         2. Extract technical specifications into a structured format.
#         3. Identify the primary product image.

#         IMAGE EXTRACTION RULES (CRITICAL):
#         1. **LOCATE**: Find the main product image URL in the HTML.
#         2. **SOURCE ATTRIBUTE**: Look for the `src` attribute inside `<img>` tags.
#         3. **IGNORE HIDDEN IMAGES**: **DO NOT** select images that have `style="display: none"` or `visibility: hidden`.
#         4. **IGNORE LOGOS**: Do not select images containing "logo", "icon", "nav", "header", "footer", or "social" in the URL.
#         5. **IGNORE ID NOIMG**: Do not select images where `id="noimg"`.
#         6. **META TAGS**: Check `<meta property="og:image" content="...">` first. If found and valid, use it.
#         7. **RELATIVE URLS**: If the image src is relative (e.g., "/images/items/6602.jpg"), construct the absolute URL using the Source URL.
#         8. **DO NOT GUESS**: Do not construct a URL if it does not exist in the HTML.
        
#         If no valid product image is found, set `image_url` to `null`.

#         ...

#         EXTRACTION_RULES:
#         - PRODUCT DETECTION: If the page describes "{mpn}", set "product_detected": true. 
#           Note: Pages often list "Related Items" or "Accessories" with different codes; ignore those, but still mark true if our target MPN is the main subject.
#         - VALUES: Only extract values visible in the text. Do not hallucinate.
#         - UNITS: Always include units (e.g., "10 lbs", "24V DC").
#         - ADDITIONAL SPECS: Extract up to 15 relevant technical specs beyond the primary list.

#         PRIMARY ATTRIBUTES TO LOOK FOR:
#         {chr(10).join([f"  • {attr}" for attr in primary_list])}
        
#         CONTENT TO ANALYZE:
#         ---
#         {content_slice}
#         ---
        
#         Return JSON following the ExtractionResponse schema.
#         """
#         return {
#             'prompt': prompt,
#             'response_model': "ExtractionResponse",
#             'estimated_tokens': 3000 
#         }
#     except Exception as e:
#         logger.error(f"build_extraction_prompt failed: {e}")
#         return None

def build_pdf_extraction_prompt(product_name:str,mpn:str,brand:str,taxonomy: str,pdf_text:str,primary_attributes:list)->dict:
    try:
        primary_list = primary_attributes[:20]
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
        5. Extract up to 15 additional specifications
        
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
            'max_tokens':2500,
            'source_type':'pdf'
        }
    except Exception as e:
        logger.error(f"pdf extraction prompt  failed str{e}")
        return