import asyncio
import logging
import os
import tempfile
import re
import json
from typing import Dict, List, Optional, Any
from app.aggregation.interfaces import IExtractor
from app.extractors import extract_pdf_pdfplumber, extract_web_playwright
from app.sacred import extract_from_web, extract_from_pdf

logger = logging.getLogger("extraction_service")

class StructuredDataExtractor(IExtractor):
    def can_handle(self, content_type: str) -> bool:
        return content_type == "html"
    
    async def extract(self, raw_bytes: bytes, url: str, prompt_config: Optional[Dict[str, Any]] = None ) -> Dict:
        html_text = raw_bytes.decode('utf-8', errors='ignore')
        json_ld_pattern = r'<script type="application/ld\+json">(.*?)</script>'
        matches = re.findall(json_ld_pattern, html_text, re.DOTALL)
        
        attributes = {}
        for match in matches:
            try:
                data = json.loads(match)
                if isinstance(data, dict):
                    if data.get('@type') == 'Product' or 'Product' in str(data.get('@type', '')):
                        attributes = self._extract_from_product_schema(data)
                        if attributes: return attributes
            except json.JSONDecodeError: continue
        return attributes

    def _extract_from_product_schema(self, data: dict) -> Dict:
        attributes = {}
        field_mapping = {'name': 'name', 'brand': 'brand', 'sku': 'sku', 'mpn': 'mpn', 'gtin': 'gtin'}
        for schema_key, attr_key in field_mapping.items():
            if schema_key in data:
                val = data[schema_key]
                attributes[attr_key] = val.get('name') if isinstance(val, dict) else str(val)
        return attributes

class HtmlExtractor(IExtractor):
    def can_handle(self, content_type: str, prompt_config: Optional[Dict[str, Any]] = None) -> bool:
        return content_type == "html"
    
    async def extract(
        self, 
        raw_bytes: bytes, 
        url: str, 
        prompt_config: Optional[Dict[str, Any]] = None
    ) -> Dict:
        
        html_text = raw_bytes.decode('utf-8', errors='ignore')
        
        custom_prompt = None
        if prompt_config:
            custom_prompt = prompt_config.get('prompt') 
            mode = prompt_config.get('mode', 'unknown')
            
            if custom_prompt:
                logger.info(f"Using CUSTOM prompt in {mode} mode")
            else:
                logger.warning(f"No custom prompt in prompt_config, using default")
        
        taxonomy = prompt_config.get('taxonomy') if prompt_config else None
        sku = (prompt_config.get('mpn') or prompt_config.get('sku') or "") if prompt_config else ""
        
        try:
            data = await asyncio.to_thread(
                extract_from_web, 
                html_text, 
                sku=sku, 
                taxonomy=taxonomy,
                custom_prompt=custom_prompt  
            )
            return data or {}
        except Exception as e:
            logger.warning(f"HTML extraction failed: {e}")
            return {}
class PlaywrightExtractor(IExtractor):
    def can_handle(self, content_type: str) -> bool:
        return content_type == "html"
    
    async def extract(self, raw_bytes: bytes, url: str, prompt_config: Optional[Dict[str, Any]] = None ) -> Dict:
        try:
            
            if "amazon." in url:
                return {}

            logger.info(f" Trying Playwright for {url}")
            html_content = await extract_web_playwright(url)
            if not html_content: return {}
            
            
            taxonomy = prompt_config.get('taxonomy') if prompt_config else None
            sku = (prompt_config.get('mpn') or prompt_config.get('sku') or "") if prompt_config else ""

            data = await asyncio.to_thread(extract_from_web, html_content, sku=sku, taxonomy=taxonomy)
            return data or {}
        except Exception as e:
            logger.error(f"Playwright extraction failed: {e}")
            return {}

class PdfExtractor(IExtractor):
    def can_handle(self, content_type: str) -> bool:
        return content_type == "pdf"
    
    async def extract(self, raw_bytes: bytes, url: str, prompt_config: Optional[Dict[str, Any]] = None ) -> Dict:
        def _extract(raw_bytes: bytes, taxonomy_str: str) -> Dict:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(raw_bytes)
                tmp_path = tmp.name
            try:
                raw_text = extract_pdf_pdfplumber(tmp_path)
                
                return extract_from_pdf(raw_text)
            finally:
                os.unlink(tmp_path)
        
        taxonomy = prompt_config.get('taxonomy', "") if prompt_config else ""
        try:
            data = await asyncio.to_thread(_extract, raw_bytes, taxonomy)
            return data or {}
        except Exception as e:
            logger.warning(f"PDF extraction failed: {e}")
            return {}

class ExtractionService:
    def __init__(self, extractors: List[IExtractor]):
        self._extractors = extractors
    
    async def extract(self, source: Dict, prompt_config: Optional[Dict[str, Any]] = None) -> Optional[Dict]:
        content_type = source.get("type", "html")
        url = source.get("source_url", "unknown")
        
        if content_type == "pdf":
            for extractor in self._extractors:
                if isinstance(extractor, PdfExtractor):
                    return await extractor.extract(source["raw_bytes"], url, prompt_config=prompt_config)
        
        extraction_order = [StructuredDataExtractor, HtmlExtractor, PlaywrightExtractor]
        
        for extractor_class in extraction_order:
            for extractor in self._extractors:
                if isinstance(extractor, extractor_class):
                    try:
                        data = await extractor.extract(source["raw_bytes"], url, prompt_config=prompt_config)
                        if data and data.get('attributes'):
                            return data
                    except Exception: continue
        return None