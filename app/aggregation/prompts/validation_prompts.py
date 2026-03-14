def build_validation_prompt(excel_attributes:dict,web_attributes:dict,mpn:str,taxonomy:str)->dict:
    try:
        comparisons=[]
        for attr_name in excel_attributes.keys():
            excel_val=excel_attributes[attr_name]
            web_val=web_attributes.get(attr_name,"NOT FOUND")
            comparisons.append(f"  • {attr_name}:")
            comparisons.append(f"      Excel: {excel_val}")
            comparisons.append(f"      Web:   {web_val}")
        comparison_text='\n'.join(comparisons)
        prompt=f"""
        You are a product data validation engine.
        PRODUCT:
        - MPN :{mpn}
        - Category:{taxonomy}
        TASK: Compare Excel data vs Web data and determine which is correct
        ATTRIBUTE_COMPARISONS:
        {comparison_text}
        VALIDATION LOGIC:
        1.EXACT MATCH -> 'matches':true,'recommendation':'keep_excel'
        2. EQUIVALENT (e.g., "1kg" vs "1000g") → "matches": true
        3. WEB MORE SPECIFIC (e.g., Excel "5V", Web "5.0V DC") → "recommendation": "use_web"
        4. CONFLICT (e.g., Excel "12V", Web "24V") → "recommendation": "manual_review"
        5. WEB NOT_FOUND → "recommendation": "keep_excel" (unless Excel is placeholder)
        
        INTERNAL CODES
        If attribute looks like internal tracking (e.g., "Bin Code", "Internal SKU"):
        - "recommendation": "keep_excel" (don't validate these)
        CONFIDENCE_SCORING:
        - 1.0: Exact match or equivalent
        - 0.9: Minor formatting differences
        - 0.7: One source more detailed than other
        - 0.5: Conflicting values
        - 0.3: Suspicious mismatch
        
        For each attribute,provide clear reasoning for your recommendation
        Return JSON following ValidationResponse schema.
        Calculate overall match_rate (% of exact matches).
        """
        return {
            'prompt':prompt,
            'response_schema':"ValidationResponse",
            'max_tokens':2000
        }
    except Exception as e:
        raise e