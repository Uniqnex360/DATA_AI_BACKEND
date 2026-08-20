from bs4 import BeautifulSoup
import logging
from typing import Optional, List
logger = logging.getLogger('extraction_prompts')

SYMBOL_STRIPPING_RULE = """\
═══════════════════════════════════════════════════════
SYMBOL STRIPPING — TRADEMARK/LEGAL MARKS
═══════════════════════════════════════════════════════
Remove trademark and legal marking symbols — ®, ™, ©, ℠, and similar —
from ALL extracted text: product_type, short_description,
long_description, features, and every attribute name and value.
- Keep the surrounding text intact; only the symbol itself is removed.
- Do not add a space where the symbol was removed if none existed.
- Do not leave double spaces behind after removal.
- Example: "Whirlpool® 6TH SENSE™ Technology" → "Whirlpool 6TH SENSE Technology"
- Example: "3M™ Scotch-Brite®" → "3M Scotch-Brite"
This applies regardless of source — HTML text, JSON-LD data, spec tables, or PDFs.
═══════════════════════════════════════════════════════"""

def extract_high_signal_specs(html_content: str, max_sections: int = 25) -> str:
    soup = BeautifulSoup(html_content, "html.parser")
    json_data = []
    for script in soup.find_all("script"):
        if script.get("type") in ["application/ld+json", "application/json"] or script.get("id") == "__NEXT_DATA__":
            content = script.string
            if content and len(content) > 100:
                json_data.append(content)
    import json as _json
    for jd in json_data[:]:
        try:
            data = _json.loads(jd)
            props = data.get('additionalProperty', [])
            if props:
                lines = []
                for p in props:
                    name = p.get('name', '')
                    value = p.get('value', '')
                    if name and value:
                        lines.append(f"{name}: {value}")
                if lines:
                    json_data.append('\n'.join(lines))
        except:
            pass
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    sections = []
    if json_data:
        json_data_sorted = sorted(json_data, key=len)
        for jd in json_data_sorted:
            if len(jd) < 50000:
                sections.append(jd)
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) >= 3:
            sections.append(str(table))
    for dl in soup.find_all("dl"):
        if len(dl.find_all("dt")) >= 2:
            sections.append(str(dl))
    for div in soup.find_all("div"):
        children = div.find_all(["div", "span", "p"], recursive=False)
        if len(children) >= 4:
            texts = [c.get_text(strip=True)
                     for c in children if c.get_text(strip=True)]
            if len(texts) >= 4 and len(" ".join(texts)) < 6000:
                sections.append(str(div))
    feature_keywords = ["feature", "highlight",
                        "benefit", "selling-point", "key-point"]
    for section in soup.find_all(['section', 'div']):
        elem_id = (section.get('id') or '').lower()
        elem_class = ' '.join(section.get('class') or []).lower()
        combined = f"{elem_id} {elem_class}"
        if any(k in combined for k in feature_keywords):
            text = section.get_text(separator='\n', strip=True)
            if 100 < len(text) < 15000:
                sections.append(str(section))
                logger.info(
                    f"Captured features section: id='{elem_id}' class='{elem_class}' len={len(text)}")
    unique_sections = list(dict.fromkeys(sections))
    content = "\n\n".join(unique_sections[:max_sections])
    non_json_content = "\n\n".join(
        s for s in unique_sections[:max_sections] if not s.strip().startswith('{'))
    if len(non_json_content.strip()) < 5000 and len(html_content) > 100000:
        raw_text = soup.get_text(separator="\n", strip=True)
        if len(raw_text) > 100:
            content = f"RAW PAGE TEXT (Fallback):\n{raw_text[:50000]}"
            logger.info(
                f"Using raw text fallback: {len(raw_text[:50000])} chars")
    MAX_CHARS = 120000
    return content[:MAX_CHARS]


def extract_product_descriptions(html_content: str) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_content, "html.parser")
    text_parts = []
    for tag in soup.find_all(['script', 'style', 'nav', 'footer']):
        tag.decompose()
    meta_tag = soup.find('meta', attrs={'name': 'description'})
    if meta_tag:
        meta_desc = meta_tag.get('content', '').strip()
        if meta_desc:
            text_parts.append(meta_desc)
    description_selectors = [
        {'class': lambda x: x and any(k in x.lower() for k in [
            'description', 'overview', 'details', 'about',
            'product-info', 'specs', 'specification',
            'feature', 'highlight', 'benefit'
        ])},
        {'id': lambda x: x and any(k in x.lower() for k in [
            'description', 'overview', 'details', 'specs',
            'feature', 'highlight', 'benefit'
        ])},
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
    if len(' '.join(text_parts)) < 300:
        main_content = soup.find('main') or soup.find(
            'article') or soup.find('body')
        if main_content:
            for tag in main_content.find_all(['p', 'li', 'dd']):
                text = tag.get_text(strip=True)
                if (text and len(text) > 10
                    and not any(skip in text.lower() for skip in ['cookie', 'subscribe', 'newsletter', 'contact us', 'privacy', 'terms'])
                        and text[:150] not in seen_texts):
                    text_parts.append(text)
                    seen_texts.add(text[:150])
    desc_text = ''.join(text_parts)
    if len(desc_text.strip()) < 100:
        raw_text = soup.get_text(separator=" ", strip=True)
        if len(raw_text) > 100:
            desc_text = raw_text
    return desc_text[:10000]


def try_paired_feature_benefit_lists(soup) -> List[str]:
    headings = soup.find_all(
        ['h1', 'h2', 'h3', 'h4', 'h5', 'p', 'strong', 'b'])
    features_list, benefits_list = None, None
    for h in headings:
        text = (h.get_text(strip=True) or '').lower().rstrip(':').strip()
        if text in ('feature', 'features'):
            candidate = h.find_next(['ul', 'ol'])
            if candidate:
                features_list = [
                    # fixed: 'separator' not 'seperator'
                    li.get_text(separator=' ', strip=True)
                    # fixed: recursive=False
                    for li in candidate.find_all('li', recursive=False)
                ]
        elif text in ('benefit', 'benefits'):
            candidate = h.find_next(['ul', 'ol'])
            if candidate:
                benefits_list = [
                    li.get_text(separator=' ', strip=True)
                    for li in candidate.find_all('li', recursive=False)
                ]
    if features_list is not None:
        features_list = [f for f in features_list if f and f.strip()]
    if benefits_list is not None:
        benefits_list = [b for b in benefits_list if b and b.strip()]
    if features_list and benefits_list and len(features_list) == len(benefits_list):
        logger.info(
            f"Paired feature/benefit lists detected: {len(features_list)} pairs")
        # fixed: real f-string
        return [f"{f} — {b}" for f, b in zip(features_list, benefits_list)]
    logger.debug(
        f"Feature/Benefit pairing skipped: "
        f"feature_list={len(features_list or [])}, benefit_list={len(benefits_list or [])} (count mismatch or missing)"
    )
    return []


def extract_features_section(html_content: str, max_features: int = 20, max_li_search: int = 1000) -> List[str]:
    if not html_content:
        logger.warning("extract_features_section: Empty html_content provided")
        return []
    if len(html_content) < 500:
        logger.debug(
            "extract_features_section: HTML too small (<500 chars), skipping feature extraction")
        return []
    try:
        soup = BeautifulSoup(html_content, "html.parser")
    except Exception as e:
        logger.error(
            f"extract_features_section: BeautifulSoup parse failed: {e}")
        return []
    features = []
    seen = set()
    try:
        paired = try_paired_feature_benefit_lists(soup)
        if paired:
            logger.info(
                f"Strategy 0 success:{len(paired)} feature/benefit strings")
            return paired
        logger.debug("Strategy 1: Searching for feature headings...")
        strategy1_found = False
        try:
            headings = soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5'])
            if not headings:
                logger.debug("Strategy 1: No headings found")
            for heading in headings:
                try:
                    heading_text = (heading.get_text(strip=True) or '').lower()
                    if 'feature' in heading_text or 'benefit' in heading_text or 'highlight' in heading_text:
                        logger.info(
                            f"Strategy 1: Found feature heading: '{heading_text}'")
                        strategy1_found = True
                        ul = heading.find_next(['ul', 'ol'])
                        if ul:
                            lis = ul.find_all('li', recursive=False)[
                                :max_li_search]
                            for li in lis:
                                try:
                                    feature_text = li.get_text(
                                        separator=' ', strip=True)
                                    if (feature_text and
                                        len(feature_text) > 10 and
                                        len(feature_text) < 500 and
                                            feature_text not in seen):
                                        features.append(feature_text)
                                        seen.add(feature_text)
                                        logger.debug(
                                            f"  ✓ Added feature: {feature_text[:60]}...")
                                except Exception as li_err:
                                    logger.debug(
                                        f"Strategy 1: Failed to extract <li>: {li_err}")
                                    continue
                            if features:
                                logger.info(
                                    f"Strategy 1: Extracted {len(features)} features from heading '{heading_text}'")
                        else:
                            logger.debug(
                                f"Strategy 1: No <ul>/<ol> found after heading '{heading_text}'")
                except Exception as heading_err:
                    logger.debug(
                        f"Strategy 1: Error processing heading: {heading_err}")
                    continue
            if strategy1_found and features:
                logger.info(
                    f"Strategy 1 SUCCESS: {len(features)} features extracted")
                return features
        except Exception as strategy1_err:
            logger.warning(f"Strategy 1 failed: {strategy1_err}")
        logger.debug(
            "Strategy 2: Searching for feature sections by class/id...")
        strategy2_found = False
        try:
            feature_sections = soup.find_all(['section', 'div'],
                                             attrs={'class': lambda x: x and 'feature' in x.lower()})
            if not feature_sections:
                feature_sections.extend(soup.find_all(['section', 'div'],
                                                      attrs={'id': lambda x: x and 'feature' in x.lower()}))
            if not feature_sections:
                logger.debug(
                    "Strategy 2: No feature sections found by class/id")
            for feature_section in feature_sections:
                try:
                    section_id = feature_section.get('id', '')
                    section_class = ' '.join(feature_section.get('class', []))
                    logger.debug(
                        f"Strategy 2: Found feature section - id='{section_id}' class='{section_class}'")
                    lis = feature_section.find_all('li')[:max_li_search]
                    for li in lis:
                        try:
                            feature_text = li.get_text(
                                separator=' ', strip=True)
                            if (feature_text and
                                len(feature_text) > 10 and
                                len(feature_text) < 500 and
                                    feature_text not in seen):
                                features.append(feature_text)
                                seen.add(feature_text)
                                strategy2_found = True
                                logger.debug(
                                    f"  ✓ Added feature: {feature_text[:60]}...")
                        except Exception as li_err:
                            logger.debug(
                                f"Strategy 2: Failed to extract <li>: {li_err}")
                            continue
                except Exception as section_err:
                    logger.debug(
                        f"Strategy 2: Error processing section: {section_err}")
                    continue
            if strategy2_found:
                logger.info(
                    f"Strategy 2 SUCCESS: {len(features)} features extracted")
                return features
        except Exception as strategy2_err:
            logger.warning(f"Strategy 2 failed: {strategy2_err}")
        logger.debug(
            "Strategy 3: Searching for lists with feature context in parents...")
        strategy3_found = False
        try:
            all_lists = soup.find_all(['ul', 'ol'])
            logger.debug(f"Strategy 3: Found {len(all_lists)} ul/ol elements")
            for ul_idx, ul in enumerate(all_lists[:100]):
                try:
                    parent = ul.find_parent()
                    parent_text = ""
                    levels = 0
                    while parent and levels < 3:
                        try:
                            parent_text += (parent.get_text(strip=True)
                                            or '').lower() + " "
                            parent = parent.find_parent()
                            levels += 1
                        except Exception as parent_err:
                            logger.debug(
                                f"Strategy 3: Error traversing parent hierarchy: {parent_err}")
                            break
                    feature_keywords = [
                        'feature', 'benefit', 'highlight', 'why choose', 'key point', 'selling point']
                    has_feature_context = any(
                        k in parent_text for k in feature_keywords)
                    if has_feature_context:
                        logger.debug(
                            f"Strategy 3: Found ul/ol with feature context at list index {ul_idx}")
                        lis = ul.find_all('li', recursive=False)[
                            :max_li_search]
                        for li in lis:
                            try:
                                feature_text = li.get_text(
                                    separator=' ', strip=True)
                                if (feature_text and
                                    len(feature_text) > 10 and
                                    len(feature_text) < 500 and
                                        feature_text not in seen):
                                    features.append(feature_text)
                                    seen.add(feature_text)
                                    strategy3_found = True
                                    logger.debug(
                                        f"  ✓ Added feature: {feature_text[:60]}...")
                            except Exception as li_err:
                                logger.debug(
                                    f"Strategy 3: Failed to extract <li>: {li_err}")
                                continue
                        if features:
                            logger.info(
                                f"Strategy 3 SUCCESS: {len(features)} features extracted")
                            return features
                except Exception as ul_err:
                    logger.debug(
                        f"Strategy 3: Error processing ul/ol: {ul_err}")
                    continue
            if not strategy3_found:
                logger.debug(
                    "Strategy 3: No features found via parent context")
        except Exception as strategy3_err:
            logger.warning(f"Strategy 3 failed: {strategy3_err}")
        if not features:
            logger.info(
                "⚠️  extract_features_section: No features extracted from any strategy")
            return []
        if len(features) > max_features:
            logger.warning(
                f"Feature extraction exceeded max ({len(features)} > {max_features}). Truncating to {max_features}.")
            features = features[:max_features]
        logger.info(
            f"✓ extract_features_section: Successfully extracted {len(features)} features")
        return features
    except Exception as e:
        logger.error(
            f"extract_features_section: Unexpected error: {e}", exc_info=True)
        return []


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
        logger.info(f"[SPEC DEBUG] Captured sections count: {len(spec_content.split('\\n\\n'))}")
        logger.info(f"[SPEC DEBUG] First 1000 chars: {spec_content[:1000]}")
        logger.info(f"[SPEC DEBUG] Contains 'Product Height': {'Product Height' in spec_content}")
        logger.info(f"[SPEC DEBUG] Contains 'Package Quantity': {'Package Quantity' in spec_content}")
        desc_text = extract_product_descriptions(html_content)
        features_list = extract_features_section(html_content)
        features_section = ""
        if features_list:
            features_section = "\n\nPRE-EXTRACTED FEATURES FROM PAGE:\n"
            logger.info(f"[FEATURES DEBUG] {source_url} => {features_list}")
            for feat in features_list:
                features_section += f"  • {feat}\n"
            logger.info(f"Pre-extracted {len(features_list)} features for LLM")
        logger.info(
            f"[Extraction Debug] Spec content length: {len(spec_content)}, Desc content length: {len(desc_text)}")
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
        - Formulation, Ingredients, Excluded materials/allergens
        - Maximum Working Pressure, Output Per Stroke, Pump Material, etc.
        - Material types, dimensions, performance specs, compatibility info
        - NO LIMIT on how many attributes to extract
        **DO NOT stop after finding priority attributes. Keep extracting ALL specs.**
        ═══════════════════════════════════════════════════════
        CRITICAL: SHIPPING/FULFILLMENT EXCLUSION — HIGHEST PRIORITY
        ═══════════════════════════════════════════════════════
        Before extracting ANY attribute, check its name:
        - If the attribute name contains any of these words: Shipping, Ship, Delivery, Freight, Transit, Handling
        - SKIP IT. Do NOT extract it. Do NOT include it in output.
        - These are fulfillment/logistics metadata, NOT product specifications.
        - This rule OVERRIDES all other extraction instructions.
        - Example: "Ship Weight: 13.6 lb" → SKIP, do not extract
        - Example: "Shipping Weight: 13.5 lb" → SKIP, do not extract
        - Example: "Product Weight: 11.5 lb" → EXTRACT (product spec, not shipping)
        {SYMBOL_STRIPPING_RULE}
        ═══════════════════════════════════════════════════════════════════
        CRITICAL INSTRUCTION: SEMANTIC ATTRIBUTE MAPPING
        ═══════════════════════════════════════════════════════════════════
        ═══════════════════════════════════════════════════════
CRITICAL: DISTRIBUTOR/RETAILER METADATA EXCLUSION
═══════════════════════════════════════════════════════
The following fields are distributor, retailer, customer-service, or
website metadata. They are NOT product technical specifications.

DO NOT extract:
- Item #
- Item No.
- Item Number
- Distributor Item Number
- Retailer Item Number
- Return Fee
- Return Fees
- Restocking Fee
- Return Method
- Return Policy
- Returns Policy
- Refund Policy
- Rating when it means customer/review/store rating
- Customer Rating
- Average Rating
- Star Rating
- Review Rating
- Seller Rating
- Store Rating
- In Stock
- Stock Status
- Availability
- Inventory Status
- Out of Stock
- Stock Level
- Units Available

Examples:
- "Item #: 5395850" → SKIP
- "Return Fees: 15%" → SKIP
- "Return Method: Mail" → SKIP
- "Return Policy: 30 Days" → SKIP
- "Rating: 4.8 out of 5" → SKIP
- "Customer Rating: 4.5 Stars" → SKIP
- "In Stock: Yes" → SKIP
- "Availability: 12 units" → SKIP
- "Stock Status: Available" → SKIP

IMPORTANT:
Do not confuse customer/store ratings with technical ratings.

Technical product ratings must still be extracted:
- "Voltage Rating: 600 V" → EXTRACT
- "Pressure Rating: 150 psi" → EXTRACT
- "IP Rating: IP65" → EXTRACT
- "Fire Rating: Class A" → EXTRACT
- "Power Rating: 100 W" → EXTRACT

This exclusion overrides instructions to extract all attributes.
═══════════════════════════════════════════════════════
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
        - Customer reviews, ratings, review body, review dates, author names
        - Q&A sections (questions and answers from customer Q&A)
        - Product overview/summary text (already in descriptions)
        - Pricing, availability
        - Product category (already in context)
        - Internal SKUs/codes (unless in PRIMARY ATTRIBUTES)
        - Customer reviews or ratings
        - Barcode numbers  
        - Division/Department codes  
        - Manufacturer/Company addresses, phone numbers, contact info
        VALUE RULES:
          - Only extract values you SEE in the content
          - Do NOT calculate, estimate, or infer
          - Always include units: "100ml" not "100"
          - JSON-LD DATA: When you see JSON content with "additionalProperty" arrays,
            extract every "name":"value" pair as an attribute.
            These are official product specs from structured data.
          - If range given, extract the range: "10-15 kg" not "12.5 kg"
          - For compound measurements like "3/8 x 15" or "1/2 x 25 ft", extract BOTH parts:
            If attribute is "Hose": value="3/8 in x 15 ft"
            Do NOT drop the diameter part
          - Compound dimensions with "x" separator:
          "3/8 x 15 ft" → value: "0.375 x 15", unit: "in x ft"
          "1/2 x 25 in" → value: "0.5 x 25", unit: "in x in"
        ADDITIONAL ATTRIBUTES:
          - Extract ALL relevant specs beyond PRIMARY ATTRIBUTES (no maximum)
          - Use the exact names from HTML for these
          - Prioritize technical/measurable attributes
        PRODUCT VERIFICATION:
        - Verify content is about "{mpn}"
        - If dominated by OTHER product codes → set "product_detected": false
        - DO NOT extract: Brand, MPN, Category, Division, Shipping info
        - ONLY extract technical product specifications
        ═══════════════════════════════════════════════════════════════════
        ═══════════════════════════════════════════════════════
🚫 FEATURES EXCLUSION RULES — CHECK FIRST, ALWAYS 🚫
═══════════════════════════════════════════════════════
BEFORE extracting ANY text as a "feature", check if it contains these topics:

❌ SHIPPING: "Free Shipping", "Ships same day", "Delivery", "$50 ship free"
❌ FINANCING: "Revolving Financing", "29.99%", "Payment options", "Installments"  
❌ STOCK: "In Stock", "Available", "Inventory Status"
❌ CUSTOMER SERVICE: "Chat with Experts", "Phone support", "Live Chat"
❌ POLICIES: "Right Part Pledge", "Hassle Free Returns", "90-day returns"

IF ANY of these topics appear → SKIP that text entirely. 
These are website boilerplate, NOT product features.

✅ ONLY extract actual product characteristics:
- "16-inch diameter wheel assembly"
- "Replaces part numbers 734-0591, 734-0765"  
- "Heavy-duty steel construction"
- "Fits Cub Cadet lawn tractors"

REMEMBER: Product features describe the PHYSICAL ITEM, not the shopping experience.
═══════════════════════════════════════════════════════
       CONTENT FOR EXTRACTION:
       {features_section} 
        {spec_content}
        {candidate_section}
        ═══════════════════════════════════════════════════════════════════
        DESCRIPTION EXTRACTION
        ═══════════════════════════════════════════════════════════════════
        ═══════════════════════════════════════════════════════════════════
        IDENTIFIER EXTRACTION (UPC / EAN / GTIN)
        ═══════════════════════════════════════════════════════════════════
        Look for product identifier codes anywhere on the page: spec tables,
        description text, meta tags, JSON-LD ("gtin", "gtin12", "gtin13").
        - UPC is a 12-digit number, often labeled "UPC", "UPC Code", "GTIN-12"
        - EAN is a 13-digit number, often labeled "EAN", "GTIN-13"
        - GTIN may appear generically as "GTIN" without specifying 12 or 13 digits
        RULES:
        - Extract ONLY digits actually present on the page. Do not calculate,
        derive, or pad one from another.
        - If a code is embedded inside a longer text blob (e.g. "Unit: EA UPC:
        077089017397 Model Number: HD 1521-0200"), pull out ONLY the digit
        sequence for that field — do not copy the surrounding text.
        - Do NOT include these codes in short_description or long_description.
        Strip them out of any description text before writing it to those fields.
        - If not found, set to null. Do not guess or hallucinate.
        Extract product descriptions from the page:
        SHORT DESCRIPTION (short_description):
        - Look for: <meta name="description"> tag content
        - Product overview/summary paragraphs (usually at top of page)
        - "About this item" section (first 1-2 sentences)
        - Keep it concise: 1-2 sentences max
        LONG DESCRIPTION (long_description):
        - Look for: "Product details", "Description", "Overview" sections
        - Detailed product information paragraphs (NOT feature bullet lists)
        - Can be longer (3-5 sentences)
        - DO NOT include feature bullet point lists here
        FEATURES (features):
        A "feature" is a SHORT, BENEFIT-ORIENTED statement about the product that 
        would appear on a buyer's product page, packaging, or marketing material.
        STEP 1 — DETECT A FEATURES SECTION ON THE PAGE:
        Look for any of these structural patterns:
          a) A container (div, section, ul, article) with class/id/aria-label 
            containing words like: feature, highlight, benefit, key-point, 
            selling-point, why-choose, at-a-glance
          b) A heading (h2/h3/h4) with text like: "Features", "Key Features", 
            "Highlights", "Benefits", "Why Choose", "At a Glance", "What Makes 
            This Special", "Product Features"
          c) A bulleted list (<ul><li>) that appears AFTER a "Features" or 
            similar heading
          d) Labeled paragraphs: <p><strong>LABEL:</strong> description</p> 
            repeated 3+ times in sequence (e.g., TYPE:/APPLICATION:/LENGTH:)
          e) Tabbed/accordion content where one tab is labeled "Features" or 
            "Highlights"
          f) TWO SEPARATE LABELED LISTS — one under a "Features:" (or "Features")
        heading, another under a "Benefit:" (or "Benefits") heading — where
        both lists have the SAME number of items, listed in the SAME order.
        This means item 1 of Features pairs with item 1 of Benefits, item 2
        with item 2, etc. Combine each pair into ONE feature string:
        "Designed and wired to dissipate electrostatic discharge away from
            the operator, directly to an earth ground — Prevents high-current shock"
        Do NOT output the Features list and Benefits list as separate,
        unrelated entries. If item counts don't match, treat only the
        Features list as the features (ignore the mismatched Benefits list
        rather than guessing pairing).
        If you detect any of these patterns, this is the features source.
        Do NOT use these as features:
          - The product spec table (key-value specs like "Diameter: 0.131")
          - The main product description paragraph
          - Customer reviews or Q&A
          - Shipping/availability/pricing info
        STEP 2 — EXTRACT EACH FEATURE AS A SEPARATE STRING:
        For each feature found:
          - Preserve the ORIGINAL WORDING as much as possible (do NOT paraphrase)
          - Keep the label if present (e.g., "TYPE: 21 Degree..." or "APPLICATION: ...")
          - One feature = one logical statement
          - If a bullet is very long (>25 words), keep it as ONE feature 
            (do not artificially split)
          - Strip HTML tags, but keep the textual content intact
        STEP 3 — DECISION TREE (apply in order):
          1. If a dedicated Features section exists → extract all items from it
          2. Else if labeled paragraphs (LABEL: value pattern) appear 3+ times → 
            extract those as features
          3. Else if a bulleted list exists with product benefits (not specs, not 
            navigation) → extract those
          4. Else → return empty array []
        QUALITY RULES:
          - Each feature: 4-30 words
          - Include 3-8 features when available
          - If only 1-2 features exist, return what you find
          - If 10+ features exist, include ALL of them (no artificial cap)
          - DO NOT invent features not on the page
          - DO NOT duplicate the same feature with slight wording changes
        EDGE CASES:
          - If the page has "Features:" followed by repeating the spec table in 
            narrative form → those narrative statements ARE features, extract them
          - If "Features" section just lists navigation links (e.g., "Warranty > 
            Register > Support") → that's NOT features, return empty
          - If the only "feature" is the product name or brand → return empty
          - If features contain the MPN repeated → still include if it's the page's 
            actual feature text
        RULES:
        - ONLY extract descriptions that exist on the page
        - Do NOT generate or create descriptions from attributes
        - If no description found, set both to null
        - Preserve the original wording as much as possible
        - Remove any HTML tags from extracted text
        ═══════════════════════════════════════════════════════════════════
        IMAGE EXTRACTION - CRITICAL RULES
        ═══════════════════════════════════════════════════════════════════
        Find ALL images of THIS SPECIFIC PRODUCT (not just one). Follow these
        rules STRICTLY:
        1. WHERE TO LOOK (a page image counts as "this product's image" only
           if it matches ONE of these):
          a) It sits inside the main product image gallery/carousel — the
             same DOM container/section as the primary <img> (class/id
             containing: product, main, hero, primary, gallery, carousel).
          b) Its src, data-zoom, data-image, or alt-text contains the MPN
             "{mpn}" or a close variant of the product name.
          c) It is the <meta property="og:image"> or <meta name="twitter:image">
             tag content (always counts as this product's primary image).
        2. DO NOT INCLUDE:
          - Related products, "customers also bought", cross-sell, upsell,
            or recommended-product images
          - Brand/manufacturer logos, site header/footer/nav images
          - Customer review photos or Q&A images
          - Banner, promotional, or advertisement images
          - Icons, badges, social-media share icons
          - SVG files, data URIs, placeholder images
        3. URL VALIDATION (applies to every image returned):
            MUST be absolute URL starting with http:// or https://
            MUST end with image extension: .jpg, .jpeg, .png, .webp, .gif
        4. URL COMPLETION:
          - If you find a URL WITHOUT extension, look for the same base URL 
            with common extensions in nearby attributes
          - Example: If src="image123", check data-zoom="image123.jpg"
        5. SIZE / DEDUPLICATION:
          - Prefer larger images: look for "large", "zoom", "1200", "hires"
          - Skip thumbnail-only duplicates of an image you already captured
            in a larger size (same product, same angle, just smaller — do
            not return both)
          - Different angles/views of the same product ARE distinct images
            — include each one
        6. OUTPUT REQUIREMENTS:
          - Return a list of COMPLETE URLs, each including extension
          - List the primary/hero image FIRST, then remaining images in
            the order they appear on the page
          - Cap the list at 8 images maximum
          - If no valid image found → return an empty list []
          - Do NOT return incomplete URLs
          - Do NOT return placeholder URLs
        EXAMPLES:
        BAD OUTPUT:
        ["https://assets.dewalt.com/.../product_image"]  (missing extension)
        GOOD OUTPUT:
        ["https://assets.dewalt.com/.../product_image.jpg"]
        BAD OUTPUT:
        ["https://example.com/logo.svg", "https://example.com/related-item-4523.jpg"]
        (logo, not product; related item, not this product)
        GOOD OUTPUT:
        ["https://example.com/products/dcf414b-main-1200x1200.jpg",
         "https://example.com/products/dcf414b-side-1200x1200.jpg",
         "https://example.com/products/dcf414b-detail-1200x1200.jpg"]
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
        OUTPUT SCHEMA — REQUIRED FIELDS
        ═══════════════════════════════════════════════════════════════════
        Return JSON in this EXACT structure. ALL fields are REQUIRED:
        {{
  "product_detected": true/false,
  "product_type": "category or null",
    "image_urls": ["URL", "URL", ...],
  "short_description": "1-2 sentences or null",
  "long_description": "3-5 sentences or null",
    "upc": "12-digit UPC code or null",
  "ean": "13-digit EAN code or null",

  "features": ["feature 1", ...],
  "attributes": [
    {{"name": "Attribute Name", "value": "value", "unit": "unit or null", "confidence": 0.95}}
  ]
}}
        IMPORTANT: If PRE-EXTRACTED FEATURES are provided above, your output 
        MUST include ALL of them in the "features" array. Do NOT omit, reformat, 
        or drop any of them. They are authoritative.
        CRITICAL: The `features` field MUST be present in your JSON output, even if it's an empty array [].
        Do NOT omit it. Do NOT return null. Always return an array.
        """
        return {
            'prompt': prompt,
            'response_schema': "ExtractionResponse",
            'max_tokens': 12000
        }
    except Exception as e:
        logger.error(f"Build_extraction_prompt failed: {e}")
        return None


def build_pdf_extraction_prompt(
    product_name: str,
    mpn: str,
    brand: str,
    taxonomy: str,
    pdf_text: str,
    primary_attributes: list
) -> dict:
    try:
        primary_list = primary_attributes if primary_attributes else []
        prompt = f"""
You are a senior product data engineer extracting specifications from an official product document.
PRODUCT CONTEXT:
- MPN: {mpn}
- Brand: {brand}
- Name: {product_name}
- Category: {taxonomy}
PRIORITY ATTRIBUTES (extract these first if found):
{chr(10).join([f"  {i+1}. {attr}" for i, attr in enumerate(primary_list)])}
DOCUMENT CONTENT:
{pdf_text[:10000]}
═══════════════════════════════════════════════════════
RULE 1: STRICT EXTRACTION — NO HALLUCINATION
═══════════════════════════════════════════════════════
- Extract ONLY values that are EXPLICITLY present in the document text above.
- Do NOT use your training data to fill in missing values.
- Do NOT guess, infer, estimate, or complete partial values.
- If a value is ambiguous or unclear, skip it entirely.
- Empty output is always better than wrong output.
SKIP any row or value that contains:
- "NPD" (No Performance Determined)
- "NF" (No Failure — this is a test result category, not a value)
- "NR" (Not Required)
- "Not relevant"
- Blank/empty cells
═══════════════════════════════════════════════════════
RULE 2: INTELLIGENT DOCUMENT STRUCTURE UNDERSTANDING
═══════════════════════════════════════════════════════
PDF documents can have many different structures:
- Single specification tables
- Multiple tables for different use cases or environments
- Performance tables organized by standard/certification
- Material safety data organized by chemical component
- Technical data sheets with sections for different applications
YOUR JOB: Understand the structure first, then extract intelligently.
STEP 1 — UNDERSTAND THE DOCUMENT TYPE:
Before extracting, identify what kind of document this is:
- Is it a simple spec sheet? → Extract all specs directly.
- Does it have MULTIPLE TABLES for the same product? → Apply deduplication rules.
- Does it have sections organized by USE CASE or APPLICATION? → Read context.
- Does it have sections organized by CHEMICAL COMPONENT? → Each component is separate.
- Does it have a PERFORMANCE DECLARATION with multiple standards? → Read which standard each value belongs to.
STEP 2 — UNDERSTAND CONTEXT FOR EACH VALUE:
When you find an attribute value, ask yourself:
1. Is this value the SAME as another value I already found with the same meaning?
2. Is this value DIFFERENT from another value I found with the same attribute name?
3. Does this attribute belong to a SPECIFIC CONTEXT (use case, environment, component)?
STEP 3 — APPLY THE DEDUPLICATION PRINCIPLE:
The core principle is simple:
- SAME attribute name + SAME value = DUPLICATE → Keep only ONE
- SAME attribute name + DIFFERENT values = DIFFERENT CONTEXTS → Keep BOTH with context added to the name
- DIFFERENT attribute name + SAME meaning = SYNONYMS → Merge into ONE canonical name
═══════════════════════════════════════════════════════
RULE 3: HOW TO HANDLE REPEATED ATTRIBUTES
═══════════════════════════════════════════════════════
When the same attribute appears multiple times in the document:
CASE A — Identical Values (True Duplicates):
The document shows "Reaction to Fire: Class E" in three different sections.
All three are identical.
→ ACTION: Extract it ONCE with the most general name.
→ OUTPUT: "Reaction to Fire: Class E"
CASE B — Different Values (Different Contexts):
The document shows:
- Section 1: "Loss of Volume: ≤ 45%"
- Section 2: "Loss of Volume: ≤ 55%"
These are DIFFERENT values, so they represent DIFFERENT things.
→ ACTION: Read the surrounding context to understand what makes them different.
→ Add that context to the attribute name to distinguish them.
→ OUTPUT: "Loss of Volume (Context A): ≤ 45%"
→ OUTPUT: "Loss of Volume (Context B): ≤ 55%"
The "context" you add should come from the document itself — 
it might be an application type, a standard number, an environment, 
a material, a temperature range, or anything else that explains 
why the values are different.
CASE C — Range or Variation (Same Attribute, Multiple Valid Values):
The document shows a range or options: "Operating Temperature: -20°C to +80°C"
→ ACTION: Extract it as a single range value.
→ OUTPUT: "Operating Temperature: -20 to 80" with unit "deg C"
═══════════════════════════════════════════════════════
RULE 4: ATTRIBUTE NAMING
═══════════════════════════════════════════════════════
- Use the EXACT attribute name from the document when it is clear.
- When adding context to distinguish values (Case B above), 
  use the shortest meaningful context from the document itself.
- Do NOT invent context labels. Use ONLY what the document says.
- If the document uses abbreviations (e.g., "EN 15651-1"), 
  you may use them as context labels.
═══════════════════════════════════════════════════════
CRITICAL: SHIPPING/FULFILLMENT EXCLUSION — HIGHEST PRIORITY
═══════════════════════════════════════════════════════
Before extracting ANY attribute, check its name:
- If the attribute name contains any of these words: Shipping, Ship, Delivery, Freight, Transit, Handling
- SKIP IT. Do NOT extract it. Do NOT include it in output.
- These are fulfillment/logistics metadata, NOT product specifications.
- This rule OVERRIDES all other extraction instructions.
- Example: "Ship Weight: 13.6 lb" → SKIP, do not extract
- Example: "Shipping Weight: 13.5 lb" → SKIP, do not extract
- Example: "Product Weight: 11.5 lb" → EXTRACT (product spec, not shipping)
- Distributor/retailer Item # or Item Number
- Return fees, return methods, return policies, and refund policies
- Customer ratings, review ratings, star ratings, and seller ratings
═══════════════════════════════════════════════════════
{SYMBOL_STRIPPING_RULE}

═══════════════════════════════════════════════════════
RULE 5: WHAT TO EXTRACT
═══════════════════════════════════════════════════════
EXTRACT:
- All technical specifications with actual measured or declared values
- Physical properties (dimensions, weights, temperatures, pressures)
- Performance ratings and classifications
- Material compositions and properties
- Certifications and standards compliance values
- Capacity, output, speed, and power specifications
- All priority attributes listed above if present
- Wattage, Lumens, CRI, Dimmable status
- Tilt range, Tiltable, Light source reference
- Driver requirements, Driver type
- Box dimensions, Boxed weight, Product weight
- Cable length, Cable diameter, Cable colour
- Protection class, Energy efficiency class
- Minimum distance from lit surface
★★★ CRITICAL VOLUME MANDATE ★★★
PDF datasheets are the MOST AUTHORITATIVE source for product data.
You MUST extract EVERY single specification you find.
If the PDF has 15+ specs, you MUST return 15+ attributes.
If the PDF has 20+ specs, you MUST return 20+ attributes.
Returning only 4-5 attributes from a detailed datasheet is a CRITICAL FAILURE.
Do NOT stop after finding a few specs. Read the ENTIRE document and extract ALL specs.
★★★ END VOLUME MANDATE ★★★
DO NOT EXTRACT:
- Marketing language ("best in class", "premium")
- Company contact details, addresses, phone numbers
- Page numbers, document version numbers, dates
- Ordering codes, pricing, availability
- Testing laboratory details and certifications of the lab itself
- Signature blocks, legal disclaimers
- Any row where the value is NPD, NF, NR, or blank
═══════════════════════════════════════════════════════
RULE 6: IMAGE EXTRACTION
═══════════════════════════════════════════════════════
- If the document text contains one or more URLs ending in .jpg, .jpeg, .png, .webp, or .gif
  that clearly refer to product images, extract them into image_urls as a list of absolute URLs.
- Otherwise set image_urls to an empty list [].
- Maximum 8 URLs.
═══════════════════════════════════════════════════════
CONFIDENCE SCORING
═══════════════════════════════════════════════════════
- PDF/official documents are authoritative sources.
- Set confidence = 0.95 for all clearly stated values.
- Set confidence = 0.90 if the value required interpretation of context.
- Never set confidence above 0.99.
═══════════════════════════════════════════════════════════════════
FEATURES (features) — for PDFs:
═══════════════════════════════════════════════════════════════════
- Look in the first 2-3 pages for marketing/feature sections
- Extract bullet-point features if present
- Same decision tree as HTML: dedicated section → labeled paragraphs → bullets
- Return as JSON array of strings
- Empty array [] if no features found
- DO NOT fabricate features
═══════════════════════════════════════════════════════════════════
OUTPUT JSON SCHEMA:
{{
  "product_detected": true/false,
  "product_type": "category name or null",
    "image_urls": ["absolute URL", "..."],
  "short_description": "1-2 sentence summary or null",
  "long_description": "3-5 sentence description or null",
  "features": ["feature 1", "feature 2", ...],
  "attributes": [
    {{"name": "Attribute Name", "value": "value", "unit": "unit or null", "confidence": 0.95}}
  ]
}}
The `features` field is REQUIRED. Return [] if no features found.        """
        return {
            'prompt': prompt,
            'response_schema': 'ExtractionResponse',
            'max_tokens': 16000,
            'source_type': 'pdf'
        }
    except Exception as e:
        logger.error(f"PDF extraction prompt failed: {e}")
        return None
