import logging
logger=logging.getLogger('extraction_prompts')
def build_extraction_prompt(product_name:str,mpn:str,brand:str,taxonomy:str,primary_attributes: list,html_content:str)-> dict:
    try:
        primary_list=primary_attributes[:20]
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
        4.Extract upto 10 ADDITIONAL relevant  technical specs beyond the primary list 
        5. Use exact values with units (e.g., "1.5 kg" not "1.5")
        PRODUCT VERIFICATION:
        - First, verify the content is actually about "{mpn}"
        - If you find references to OTHER product codes, set "product_detected": false
        
        CONTENT TO ANALYZE:
        {html_content[:8000]}
IMAGE EXTRACTION (CRITICAL):
- Locate the main product image URL in the HTML. Look for:
  * `<meta property="og:image" content="...">` (Open Graph)
  * `<meta name="twitter:image" content="...">` (Twitter Card)
  * `<img>` tags with `src` containing keywords like "product", "main", "hero", "full", or the MPN `{mpn}`
  * `<link rel="image_src" href="...">`
- Ignore small icons, logos, or thumbnails.
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