import logging
from typing import Optional, List
logger = logging.getLogger('extraction_prompts')
from bs4 import BeautifulSoup
def extract_high_signal_specs(html_content: str, max_sections: int = 25) -> str:
    soup = BeautifulSoup(html_content, "html.parser")

    # Remove noise
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    sections = []

    # 1. Tables
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) >= 3:
            sections.append(str(table))

    # 2. Definition lists
    for dl in soup.find_all("dl"):
        if len(dl.find_all("dt")) >= 2:
            sections.append(str(dl))

    # 3. Structured div grids (label-value patterns)
    for div in soup.find_all("div"):
        children = div.find_all(["div", "span", "p"], recursive=False)
        if len(children) >= 4:
            texts = [c.get_text(strip=True) for c in children if c.get_text(strip=True)]
            if len(texts) >= 4 and len(" ".join(texts)) < 6000:
                sections.append(str(div))

    # 4. Section headers indicating spec areas
    for section in soup.find_all("section"):
        header = section.find(["h1", "h2", "h3", "h4"])
        if header:
            header_text = header.get_text().lower()
            if any(k in header_text for k in [
                "spec", "technical", "dimension",
                "performance", "details", "data"
            ]):
                if len(section.get_text()) < 15000:
                    sections.append(str(section))

    # Deduplicate
    unique_sections = list(dict.fromkeys(sections))

    content = "\n\n".join(unique_sections[:max_sections])

    # Hard safety cap
    MAX_CHARS = 120000
    return content[:MAX_CHARS]
def extract_product_descriptions(html_content: str) -> str:
    """Extract all product-related text from HTML with fallbacks."""
    from bs4 import BeautifulSoup
    
    soup = BeautifulSoup(html_content, "html.parser")
    text_parts = []
    
    # Remove noise early
    for tag in soup.find_all(['script', 'style', 'nav', 'footer']):
        tag.decompose()
    
    # 1. Meta description
    meta_tag = soup.find('meta', attrs={'name': 'description'})
    if meta_tag:
        meta_desc = meta_tag.get('content', '').strip()
        if meta_desc:
            text_parts.append(meta_desc)
    
    # 2. Semantic containers
    description_selectors = [
        {'class': lambda x: x and any(k in x.lower() for k in ['description', 'overview', 'details', 'about', 'product-info', 'specs', 'specification'])},
        {'id': lambda x: x and any(k in x.lower() for k in ['description', 'overview', 'details', 'specs'])},
    ]
    
    seen_texts = set()
    for selector in description_selectors:
        for tag in soup.find_all(['div', 'section', 'article'], attrs=selector):
            text = tag.get_text(strip=True)
            if text and len(text) > 50:
                text_hash = text[:150]
                if text_hash not in seen_texts:
                    text_parts.append(text)
                    seen_texts.add(text_hash)
    
    # 3. Fallback content
    if len(' '.join(text_parts)) < 300:
        main_content = soup.find('main') or soup.find('article') or soup.find('body')
        if main_content:
            for tag in main_content.find_all(['p', 'li', 'dd']):
                text = tag.get_text(strip=True)
                if (text and len(text) > 20 
                    and not any(skip in text.lower() for skip in ['cookie', 'subscribe', 'newsletter', 'contact us', 'privacy', 'terms'])
                    and text[:150] not in seen_texts):
                    text_parts.append(text)
                    seen_texts.add(text[:150])
    
    # Join all text with newlines
    desc_text = ''.join(text_parts)
    return desc_text[:3000]
  
def build_extraction_prompt(product_name: str, mpn: str, brand: str, taxonomy: str,
                            primary_attributes: list, html_content: str,
                            candidate_images: Optional[List[str]] = None, source_url: str = "") -> dict:
    try:
        domain = ""
        if source_url:
            from urllib.parse import urlparse
            domain = urlparse(source_url).netloc.lower()
        is_brand_site = False
        if brand and domain:
            brand_clean = brand.lower().replace(" ", "").replace("-", "")
            domain_clean = domain.replace("www.", "").replace(
                ".com", "").replace("-", "")
            is_brand_site = brand_clean in domain_clean
        if is_brand_site:
            confidence_instruction = """
            CONFIDENCE SCORING (OFFICIAL BRAND WEBSITE):
            - You are extracting from the manufacturer's official website
            - Set confidence = 1.0 for all attributes (maximum trust)
            - This is the primary source of truth
            """
        else:
            confidence_instruction = """
            CONFIDENCE SCORING (THIRD-PARTY RETAILER):
            - You are extracting from a reseller/retailer website
            - Set confidence = 0.85-0.95 based on clarity
            - Use 0.95 if specification is clearly stated in table
            - Use 0.90 if found in product description
            - Use 0.85 if partially visible or inferred from context
            """
        primary_list = primary_attributes if primary_attributes else []
        candidate_section = ""
        if candidate_images:
            candidate_section = "\nCANDIDATE IMAGES FROM IMAGE SEARCH:\n"
            for img_url in candidate_images[:5]:
                candidate_section += f"  - {img_url}\n"
        primary_attrs_display = "\n".join(
            [f"  {i+1}. {attr}" for i, attr in enumerate(primary_list)])
        spec_content = extract_high_signal_specs(html_content)
        desc_text = extract_product_descriptions(html_content) 


        prompt = f"""
        You are extracting technical specifications from product content.
        PRODUCT CONTEXT:
        - Name: {product_name}
        - MPN: {mpn}
        - Brand: {brand}
        - Category: {taxonomy}
           DESCRIPTION CONTENT:
        {desc_text}
        
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
        DYNAMIC ATTRIBUTE DEDUPLICATION
        ═══════════════════════════════════════════════════════════════════
        NORMALIZATION ALGORITHM (for ANY attribute, ANY category):

        1. EXTRACT: Find attribute name from HTML
        2. NORMALIZE: 
           - Convert to lowercase
           - Replace spaces/underscores/hyphens with single hyphen
           - Remove qualifiers: "Type", "Rating", "Spec", "Material", "Overall", "Total", "Nominal"
        3. MATCH against PRIMARY ATTRIBUTES:
           - Normalize each primary attribute same way
           - If normalized forms identical → use PRIMARY ATTRIBUTE name (exact spelling)
           - If no match → use HTML name
        4. OUTPUT: Return normalized name to prevent duplicates

        This algorithm works for ANY product category, ANY attribute variation.
        No hardcoded rules. Pure semantic matching via normalization.

        ══════════════════════════════════════════════════════════════════
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
        - UPC/EAN/Barcode numbers  
        - Division/Department codes  
        - Shipping dimensions unless labeled as "Product Dimensions"  
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
        - DO NOT extract: Brand, MPN, Category, UPC, Division, Shipping info
        - ONLY extract technical product specifications
        ═══════════════════════════════════════════════════════════════════
       CONTENT FOR EXTRACTION:
        {spec_content}
        {candidate_section}
        ═══════════════════════════════════════════════════════════════════
        DESCRIPTION EXTRACTION
        ═══════════════════════════════════════════════════════════════════
        Extract product descriptions from the page:

        SHORT DESCRIPTION (short_description):
        - Look for: <meta name="description"> tag content
        - Product overview/summary paragraphs (usually at top of page)
        - "About this item" section (first 1-2 sentences)
        - Keep it concise: 1-2 sentences max

        LONG DESCRIPTION (long_description):
        - Look for: "Product details", "Description", "Overview" sections
        - Feature lists, bullet points
        - Detailed product information paragraphs
        - Can be longer (3-5 sentences or bullet points)

        RULES:
        - ONLY extract descriptions that exist on the page
        - Do NOT generate or create descriptions from attributes
        - If no description found, set both to null
        - Preserve the original wording as much as possible
        - Remove any HTML tags from extracted text
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
        {confidence_instruction}
        ═══════════════════════════════════════════════════════════════════
        ═══════════════════════════════════════════════════════════════════
        OUTPUT
        ═══════════════════════════════════════════════════════════════════
        Return JSON following ExtractionResponse schema:
        - attributes: list of extracted specifications
        - product_detected: true/false
        - product_type: category if detected
        - image_url: product image URL or null
        "short_description": "Short product description (1-2 sentences) or null",
          "long_description": "Detailed product description (3-5 sentences or bullet points) or null"
        """
        return {
            'prompt': prompt,
            'response_schema': "ExtractionResponse",
            'max_tokens': 8000
        }
    except Exception as e:
        logger.error(f"Build_extraction_prompt failed: {e}")
        return None


def build_pdf_extraction_prompt(product_name: str, mpn: str, brand: str, taxonomy: str, pdf_text: str, primary_attributes: list) -> dict:
    try:
        primary_list = primary_attributes if primary_attributes else []
        prompt = f"""
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
        return {
            'prompt': prompt,
            'response_schema': 'ExtractionResponse',
            'max_tokens': 16000,
            'source_type': 'pdf'
        }
    except Exception as e:
        logger.error(f"pdf extraction prompt  failed str{e}")
        return
