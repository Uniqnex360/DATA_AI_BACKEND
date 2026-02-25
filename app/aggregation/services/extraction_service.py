

import asyncio
import logging
import os
import tempfile
import re
import json
from typing import Dict, List, Optional
from app.aggregation.interfaces import IExtractor
from app.extractors import extract_pdf_pdfplumber, extract_web_playwright
from app.sacred import extract_from_web, extract_from_pdf

logger = logging.getLogger("extraction_service")


class StructuredDataExtractor(IExtractor):
    
    def can_handle(self, content_type: str) -> bool:
        return content_type == "html"
    
    async def extract(self, raw_bytes: bytes, url: str) -> Dict:
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
                        if attributes:
                            logger.info(f" Extracted {len(attributes)} attributes from JSON-LD")
                            return attributes
                    
                    
                    for key, val in data.items():
                        if isinstance(val, dict) and val.get('@type') == 'Product':
                            attributes = self._extract_from_product_schema(val)
                            if attributes:
                                return attributes
                        
                        if isinstance(val, list):
                            for item in val:
                                if isinstance(item, dict) and item.get('@type') == 'Product':
                                    attributes = self._extract_from_product_schema(item)
                                    if attributes:
                                        return attributes
            
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse JSON-LD: {e}")
                continue
        
        return attributes
    
    def _extract_from_product_schema(self, data: dict) -> Dict:
        attributes = {}
        
        
        field_mapping = {
            'name': 'name',
            'description': 'description',
            'brand': 'brand',
            'sku': 'sku',
            'gtin': 'gtin',
            'gtin13': 'gtin13',
            'gtin14': 'gtin14',
            'mpn': 'mpn',
            'image': 'image',
            'color': 'color',
            'size': 'size',
            'weight': 'weight',
            'category': 'category',
        }
        
        for schema_key, attr_key in field_mapping.items():
            if schema_key in data:
                value = data[schema_key]
                
                
                if isinstance(value, dict):
                    attributes[attr_key] = value.get('name') or str(value)
                elif isinstance(value, list) and value:
                    attributes[attr_key] = value[0] if len(value) == 1 else ', '.join(str(v) for v in value)
                else:
                    attributes[attr_key] = str(value)
        
        
        if 'offers' in data:
            offers = data['offers']
            if isinstance(offers, dict):
                price = offers.get('price')
                if price:
                    attributes['price'] = str(price)
            elif isinstance(offers, list) and offers:
                price = offers[0].get('price')
                if price:
                    attributes['price'] = str(price)
        
        return attributes


class HtmlExtractor(IExtractor):
    
    def can_handle(self, content_type: str) -> bool:
        return content_type == "html"
    
    async def extract(self, raw_bytes: bytes, url: str) -> Dict:
        html_text = raw_bytes.decode('utf-8', errors='ignore')
        
        try:
            
            data = await asyncio.to_thread(extract_from_web, html_text)
            if data:
                logger.info(f" Extracted from HTML: {len(data.get('attributes', {}))} attributes")
            return data or {}
        except Exception as e:
            logger.warning(f"HTML extraction failed: {e}")
            return {}


class PlaywrightExtractor(IExtractor):
    
    def can_handle(self, content_type: str) -> bool:
        return content_type == "html"
    
    async def extract(self, raw_bytes: bytes, url: str) -> Dict:
        try:
            logger.info(f" Trying Playwright for {url}")
            
            
            html_content = await extract_web_playwright(url)
            
            if not html_content:
                logger.warning("Playwright returned empty content")
                return {}
            
            
            data = await asyncio.to_thread(extract_from_web, html_content)
            
            if data:
                logger.info(f" Playwright extracted {len(data.get('attributes', {}))} attributes")
            
            return data or {}
        
        except Exception as e:
            logger.error(f"Playwright extraction failed: {e}")
            return {}


class PdfExtractor(IExtractor):
    
    def can_handle(self, content_type: str) -> bool:
        return content_type == "pdf"
    
    async def extract(self, raw_bytes: bytes, url: str) -> Dict:
        def _extract(raw_bytes: bytes) -> Dict:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(raw_bytes)
                tmp_path = tmp.name
            try:
                raw_text = extract_pdf_pdfplumber(tmp_path)
                return extract_from_pdf(raw_text)
            finally:
                os.unlink(tmp_path)
        
        try:
            data = await asyncio.to_thread(_extract, raw_bytes)
            if data:
                logger.info(f" PDF extracted {len(data.get('attributes', {}))} attributes")
            return data or {}
        except Exception as e:
            logger.warning(f"PDF extraction failed: {e}")
            return {}


class ExtractionService:
    
    
    def __init__(self, extractors: List[IExtractor]):
        self._extractors = extractors
    
    def register(self, extractor: IExtractor) -> None:
        self._extractors.append(extractor)
    
    async def extract(self, source: Dict) -> Optional[Dict]:
        
        content_type = source.get("type", "html")
        url = source.get("source_url", "unknown")
        
        logger.info(f" Extracting from {url}")
        
        
        if content_type == "pdf":
            for extractor in self._extractors:
                if isinstance(extractor, PdfExtractor):
                    try:
                        data = await extractor.extract(source["raw_bytes"], url)
                        if data:
                            return data
                    except Exception as e:
                        logger.warning(f"PDF extraction failed: {e}")
            return None
        
        
        extraction_order = [
            StructuredDataExtractor,  
            HtmlExtractor,             
            PlaywrightExtractor,       
        ]
        
        for extractor_class in extraction_order:
            for extractor in self._extractors:
                if isinstance(extractor, extractor_class):
                    try:
                        logger.info(f" Trying {extractor_class.__name__}...")
                        data = await extractor.extract(source["raw_bytes"], url)
                        
                        if data and data.get('attributes'):
                            logger.info(f"{extractor_class.__name__} succeeded!")
                            return data
                        
                    except Exception as e:
                        logger.warning(f"{extractor_class.__name__} failed: {e}")
                        continue
        
        logger.warning(f" All extractors failed for {url}")
        return None