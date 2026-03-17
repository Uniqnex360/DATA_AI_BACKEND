import logging
from typing import Optional,List
logger=logging.getLogger('extraction_prompts')
def build_extraction_prompt(product_name:str,mpn:str,brand:str,taxonomy:str,primary_attributes: list,html_content:str,candidate_images: Optional[List[str]] = None)-> dict:
    try:
        primary_list=primary_attributes[:20]
        candidate_section = ""
        if candidate_images:
            candidate_section = "\nCANDIDATE IMAGES FROM IMAGE SEARCH (check if any appear on this page):\n"
            for img_url in candidate_images[:5]:
                candidate_section += f"  - {img_url}\n"
            candidate_section += (
                "If you see one of these images in the HTML and it appears to be the main product image, "
                "return its URL. If not, still look for any product image as described below.\n"
            )
        prompt=f"""
        You are a  technical specification extraction engine.
        PRODUCT_CONTEXT
        - Name : {product_name}
        - MPN  :{mpn}
        - Brand :{brand}
        - Category :{taxonomy}
        TASK: Extract technical specifications for the provided content.
        PRIMARY ATTRIBUTES(extract these first)
        {chr(10).join([f"  • {attr}" for attr in primary_list])}
        EXTRACTION_RULES:
        1.Only extract  if you see the exact value in the content
        2.Do NOT invent or estimate values
        3.Do NOT extract:
          - Website metadata (dates, IDs, tracking codes)
          - Marketing fluff("world's best","premium quality")
          - Internal codes (unless specifically requested)
          - **Category information** (e.g., fields named "Category", "Product Group") – these are already provided in the product context.
        4.Extract upto 10 ADDITIONAL relevant  technical specs beyond the primary list 
        5. Use exact values with units (e.g., "1.5 kg" not "1.5")
        PRODUCT VERIFICATION:
        - First, verify the content is actually about "{mpn}"
        - If you find references to OTHER product codes, set "product_detected": false
        
        CONTENT TO ANALYZE:
        {html_content[:8000]}
    {candidate_section}
    IMAGE EXTRACTION (CRITICAL):
    - Locate the main product image URL in the HTML. Look for:
    * `<meta property="og:image" content="...">` (Open Graph)
    * `<meta name="twitter:image" content="...">` (Twitter Card)
    * `<img>` tags with `src` containing keywords like "product", "main", "hero", "full", or the MPN `{mpn}`
    * `<link rel="image_src" href="...">`
    - Ignore small icons, logos, or thumbnails.
    **SOURCE ATTRIBUTE**: Look for the `src` attribute inside `<img>` tags.
#    **IGNORE HIDDEN IMAGES**: **DO NOT** select images that have `style="display: none"` or `visibility: hidden`.
#      **IGNORE LOGOS**: Do not select images containing "logo", "icon", "nav", "header", "footer", or "social" in the URL.
#       **IGNORE ID NOIMG**: Do not select images where `id="noimg"`.
#         **META TAGS**: Check `<meta property="og:image" content="...">` first. If found and valid, use it.
#         **RELATIVE URLS**: If the image src is relative (e.g., "/images/items/6602.jpg"), construct the absolute URL using the Source URL.
#        **DO NOT GUESS**: Do not construct a URL if it does not exist in the HTML.
    - Return the absolute URL (if relative, assume it's relative to the page domain).
    - If no product image found, set `image_url` to `null`.
                
    Return JSON following the ExtractionResponse schema with:
    - attributes: list of extracted specifications
    - product_detected: true/false
    - product_type: category if detected
    - image_url: product image URL or null

        """
        return {
            'prompt':prompt,
            'response_schema':"ExtractionResponse",
            'max_tokens':2000
        }
    except Exception as e:
        logger.error(f"Build_extraction_prompt failed str{e}")
        return

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