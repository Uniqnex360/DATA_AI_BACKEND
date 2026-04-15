import pandas as pd
import re
from typing import List, Dict, Any, Tuple, Set
from io import BytesIO
import logging
logger = logging.getLogger(__name__)
class ExcelMPNService:
    MPN_COLUMN_PATTERNS = [
        r'^mpn$',                       
        r'^mpn[_\-]?code$', 
        r'^manufacturer[_\-]?part[_\-]?number$',
        r'^part[_\-]?number$', 
        r'^part[_\-]?no$', 
        r'^model[_\-]?number$',
        r'^model[_\-]?no$',
        r'^mfr[_\-]?part[_\-]?number$',
        r'^mfg[_\-]?part[_\-]?number$',
        r'^prod[_\-]?id$',              
        r'^product[_\-]?id$',
        r'^product[_\-]?code$',
        r'^item[_\-]?id$',
        r'^item[_\-]?code$',
        r'^sku$',                       
        r'^upc$', 
        r'^ean$', 
        r'^gtin$', 
        r'^identifier$',
    ]
    BRAND_PATTERNS = [r'^brand$', r'^brand[_\-]?name$', r'^manufacturer$']
    PRODUCT_NAME_PATTERNS = [r'^product[_\-]?name$', r'^title$', r'^description$']
    MPN_MIN_LENGTH = 2
    MPN_MAX_LENGTH = 100
    MAX_FILE_SIZE = 10 * 1024 * 1024  
    MAX_MPNS_PER_FILE = 1000
    @classmethod
    def validate_file(cls, file_bytes: bytes, filename: str) -> Tuple[bool, str]:
        """Validate file before processing"""
        if len(file_bytes) > cls.MAX_FILE_SIZE:
            return False, f"File size exceeds {cls.MAX_FILE_SIZE // (1024*1024)}MB limit"
        valid_extensions = ('.xlsx', '.xls', '.csv')
        if not filename.lower().endswith(valid_extensions):
            return False, f"Invalid file type. Supported: {', '.join(valid_extensions)}"
        return True, "Valid"
    @classmethod
    def parse_mpns_from_excel(
        cls, 
        file_bytes: bytes, 
        filename: str,
        sheet_name: str = None
    ) -> Dict[str, Any]:
        """
        Parse MPNs from Excel/CSV file with intelligent column detection.
        Returns:
            {
                'mpns': List[str],
                'metadata': {
                    'total_rows': int,
                    'valid_mpns': int,
                    'invalid_mpns': int,
                    'duplicates_removed': int,
                    'mpn_column': str,
                    'brand_column': str | None,
                    'product_name_column': str | None,
                    'errors': List[str],
                    'warnings': List[str]
                }
            }
        """
        result = {
            'mpns': [],
            'metadata': {
                'total_rows': 0,
                'valid_mpns': 0,
                'invalid_mpns': 0,
                'duplicates_removed': 0,
                'empty_rows': 0,
                'mpn_column': None,
                'brand_column': None,
                'product_name_column': None,
                'errors': [],
                'warnings': []
            }
        }
        try:
            if filename.endswith('.csv'):
                df = pd.read_csv(BytesIO(file_bytes), encoding='utf-8', encoding_errors='ignore')
            else:
                df = pd.read_excel(BytesIO(file_bytes), sheet_name=sheet_name or 0)
            if df.empty:
                result['metadata']['errors'].append("Excel file is empty")
                return result
            result['metadata']['total_rows'] = len(df)
            df.columns = df.columns.str.strip().str.lower()
            mpn_column = cls._find_column(df.columns, cls.MPN_COLUMN_PATTERNS)
            
            logger.info(f"DataFrame columns: {df.columns.tolist()}")
            logger.info(f"Found MPN column: {mpn_column}")

            if mpn_column:
                sample_values = df[mpn_column].dropna().head(5).tolist()
                logger.info(f"Sample MPN values: {sample_values}")
            if not mpn_column:
                first_col = df.columns[0]
                sample_values = df[first_col].dropna().astype(str).head(10).tolist()
                if cls._looks_like_mpns(sample_values):
                    mpn_column = first_col
                    result['metadata']['warnings'].append(f"Using column '{first_col}' as MPN (auto-detected)")
                else:
                    result['metadata']['errors'].append(
                        f"Could not find MPN column. Looked for: {', '.join(cls.MPN_COLUMN_PATTERNS[:5])}..."
                    )
                    return result
            result['metadata']['mpn_column'] = mpn_column
            brand_column = cls._find_column(df.columns, cls.BRAND_PATTERNS)
            product_name_column = cls._find_column(df.columns, cls.PRODUCT_NAME_PATTERNS)
            result['metadata']['brand_column'] = brand_column
            result['metadata']['product_name_column'] = product_name_column
            mpn_data = []
            seen_mpns: Set[str] = set()
            for idx, row in df.iterrows():
                mpn_value = row[mpn_column]
                if pd.isna(mpn_value) or str(mpn_value).strip() == '':
                    result['metadata']['empty_rows'] += 1
                    continue
                mpn = str(mpn_value).strip()
                if mpn.endswith('.0') and mpn.replace('.', '').replace('0', '').isdigit():
                    mpn = mpn[:-2]
                is_valid, error = cls._validate_mpn(mpn)
                if not is_valid:
                    result['metadata']['invalid_mpns'] += 1
                    result['metadata']['errors'].append(f"Row {idx + 2}: {error}")
                    continue
                mpn_normalized = cls._normalize_mpn(mpn)
                if mpn_normalized in seen_mpns:
                    result['metadata']['duplicates_removed'] += 1
                    continue
                seen_mpns.add(mpn_normalized)
                brand = None
                product_name = None
                if brand_column and not pd.isna(row[brand_column]):
                    brand = str(row[brand_column]).strip()
                if product_name_column and not pd.isna(row[product_name_column]):
                    product_name = str(row[product_name_column]).strip()
                mpn_data.append({
                    'mpn': mpn,
                    'brand': brand,
                    'product_name': product_name
                })
            if len(mpn_data) > cls.MAX_MPNS_PER_FILE:
                mpn_data = mpn_data[:cls.MAX_MPNS_PER_FILE]
                result['metadata']['warnings'].append(
                    f"Truncated to {cls.MAX_MPNS_PER_FILE} MPNs (max limit)"
                )
            result['mpns'] = [item['mpn'] for item in mpn_data]
            result['metadata']['valid_mpns'] = len(result['mpns'])
            result['detailed_data'] = mpn_data
            logger.info(f"Parsed {len(result['mpns'])} valid MPNs from {filename}")
            return result
        except Exception as e:
            logger.error(f"Failed to parse Excel file: {e}", exc_info=True)
            result['metadata']['errors'].append(f"Parse error: {str(e)}")
            return result
    @classmethod
    def _find_column(cls, columns: List[str], patterns: List[str]) -> str | None:
        """Find column matching any of the patterns, with priority for exact 'mpn' match"""
        
        # PRIORITY 1: Exact match for 'mpn' (case-insensitive)
        for col in columns:
            if col.lower() == 'mpn':
                logger.info(f"Found exact 'mpn' column match: '{col}'")
                return col
        
        # PRIORITY 2: Check patterns in order, but skip 'sku' if we haven't found 'mpn' yet
        for col in columns:
            for pattern in patterns:
                if re.match(pattern, col, re.IGNORECASE):
                    # Skip 'sku' if we're still looking for better matches
                    # But we need to check all columns for 'mpn' patterns first
                    logger.info(f"Column '{col}' matched pattern '{pattern}'")
                    return col
        
        return None
    @classmethod
    def _looks_like_mpns(cls, values: List[str]) -> bool:
        """Heuristic to detect if values look like MPNs"""
        if not values:
            return False
        mpn_patterns = [
            r'^[A-Z0-9\-_\.]+$',  
            r'^[A-Z]{2,}\d+',     
            r'^\d+[A-Z]+',        
        ]
        matches = 0
        for value in values:
            for pattern in mpn_patterns:
                if re.match(pattern, value, re.IGNORECASE):
                    matches += 1
                    break
        return matches / len(values) > 0.6
    @classmethod
    def _validate_mpn(cls, mpn: str) -> Tuple[bool, str]:
        """Validate individual MPN"""
        
        if not mpn or mpn.strip() == '':
            return False, "MPN is empty"
        
        
        if mpn.isdigit():
            return True, "Valid"
        
        
        if len(mpn) < cls.MPN_MIN_LENGTH:
            return False, f"MPN too short (min {cls.MPN_MIN_LENGTH} chars)"
        
        if len(mpn) > cls.MPN_MAX_LENGTH:
            return False, f"MPN too long (max {cls.MPN_MAX_LENGTH} chars)"
        
        if re.search(r'[<>"\';&|]', mpn):
            return False, "MPN contains invalid characters"
        
        suspicious = [
            (r'^\s*$', "MPN is whitespace only"),
            (r'^test', "MPN looks like test data"),
            (r'^sample', "MPN looks like sample data"),
            (r'^n/?a$', "MPN is N/A"),
        ]
        
        for pattern, warning in suspicious:
            if re.match(pattern, mpn, re.IGNORECASE):
                return False, warning
        
        return True, "Valid"
    @classmethod
    def _normalize_mpn(cls, mpn: str) -> str:
        """Normalize MPN for duplicate detection"""
        return mpn.upper().strip().replace(' ', '').replace('-', '').replace('_', '')
    @classmethod
    def generate_template(cls) -> bytes:
        """Generate an Excel template for MPN upload"""
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            instructions = pd.DataFrame({
                'Instruction': [
                    '1. Fill MPN column with your Manufacturer Part Numbers',
                    '2. Brand and Product Name columns are optional',
                    '3. Do not modify column headers',
                    '4. Remove any empty rows',
                    '5. Maximum 1000 MPNs per file',
                    '',
                    'Column Descriptions:',
                    '- MPN: Required. Manufacturer Part Number',
                    '- Brand: Optional. Manufacturer/Brand name',
                    '- Product Name: Optional. Product title/description'
                ]
            })
            instructions.to_excel(writer, sheet_name='Instructions', index=False)
            template = pd.DataFrame({
                'MPN': ['EXAMPLE-001', 'ABC-1234', 'XYZ-5678'],
                'Brand': ['Example Corp', 'ABC Industries', 'XYZ Manufacturing'],
                'Product Name': ['Example Product 1', 'Widget Pro', 'Super Gadget']
            })
            template.to_excel(writer, sheet_name='MPNs', index=False)
        return output.getvalue()