"""
Production-ready PDF extraction service
"""
import io
import logging
from typing import Optional, Dict, List
import PyPDF2
import pdfplumber
import re
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
logger = logging.getLogger("pdf_service")

class PDFExtractionService:
    
    def __init__(self, max_pages: int = 10):
        self.max_pages = max_pages
   
    @staticmethod
    def find_pdf_links(html: str, base_url: str) -> List[str]:
        try:
            soup = BeautifulSoup(html, 'html.parser')
            pdf_links = []
            
            
            for link in soup.find_all('a', href=True):
                href_raw = link['href'].strip()
                
                
                parsed_path = urlparse(href_raw).path.lower()
                
                
                if not parsed_path.endswith('.pdf'):
                    continue
                
                
                full_url = urljoin(base_url, href_raw)
                
                
                link_text = (link.get_text() or '').lower()
                link_classes = ' '.join(link.get('class', [])).lower()
                href_lower = href_raw.lower()
                
                priority_keywords = [
                    'datasheet', 'data sheet', 'technical', 'spec', 'specification',
                    'tds', 'pds', 'product data', 'safety data', 'sds', 'msds'
                ]
                
                is_priority = any(
                    kw in link_text or kw in href_lower or kw in link_classes
                    for kw in priority_keywords
                )
                
                
                skip_keywords = ['brochure', 'catalog', 'flyer', 'warranty', 'installation guide']
                if any(kw in link_text or kw in href_lower for kw in skip_keywords):
                    continue
                
                if is_priority:
                    pdf_links.insert(0, full_url)
                else:
                    pdf_links.append(full_url)
            
            
            seen = set()
            unique_pdfs = []
            for pdf in pdf_links:
                if pdf not in seen:
                    seen.add(pdf)
                    unique_pdfs.append(pdf)
            
            return unique_pdfs[:3]  
            
        except Exception as e:
            logger.warning(f"Failed to parse PDF links: {e}")
            return []
    async def extract_text(self, pdf_bytes: bytes) -> Optional[str]:
       
        try:
            
            text = self._extract_with_pypdf2(pdf_bytes)
            if text and len(text.strip()) > 100:
                logger.info(f"PyPDF2 extracted {len(text)} characters")
                return text
            
            
            text = await self._extract_with_pdfplumber(pdf_bytes)
            if text and len(text.strip()) > 100:
                logger.info(f" pdfplumber extracted {len(text)} characters")
                return text
            
            logger.warning("All PDF extraction methods failed")
            return None
            
        except Exception as e:
            logger.error(f"PDF extraction failed: {e}")
            return None
    
    def _extract_with_pypdf2(self, pdf_bytes: bytes) -> Optional[str]:
        try:
            pdf_file = io.BytesIO(pdf_bytes)
            reader = PyPDF2.PdfReader(pdf_file)
            
            text_pages = []
            for i, page in enumerate(reader.pages[:self.max_pages]):
                page_text = page.extract_text()
                if page_text:
                    text_pages.append(f"--- Page {i+1} ---\n{page_text}")
            
            return "\n\n".join(text_pages) if text_pages else None
            
        except Exception as e:
            logger.info(f"PyPDF2 extraction failed: {e}")
            return None
    
    async def _extract_with_pdfplumber(self, pdf_bytes: bytes) -> Optional[str]:
        try:
            pdf_file = io.BytesIO(pdf_bytes)
            text_pages = []
            
            with pdfplumber.open(pdf_file) as pdf:
                for i, page in enumerate(pdf.pages[:self.max_pages]):
                    
                    page_text = page.extract_text() or ""
                    
                    
                    tables = page.extract_tables()
                    table_text = ""
                    for table in tables:
                        if table:
                            for row in table:
                                if row and any(cell for cell in row if cell):
                                    table_text += " | ".join([str(cell or "") for cell in row]) + "\n"
                    
                    
                    if page_text or table_text:
                        text_pages.append(f"--- Page {i+1} ---\n{page_text}\n{table_text}")
            
            return "\n\n".join(text_pages) if text_pages else None
            
        except Exception as e:
            logger.info(f"pdfplumber extraction failed: {e}")
            return None
    
    def extract_specs_from_text(self, text: str) -> Dict[str, str]:
        
        specs = {}
        
        
        pattern1 = r'([A-Za-z][A-Za-z\s\-]{2,50}):\s*([^\n\r]{1,200})'
        matches = re.findall(pattern1, text, re.MULTILINE)
        for key, value in matches:
            specs[key.strip()] = value.strip()
        
        
        pattern2 = r'([A-Za-z][A-Za-z\s\-]{2,50})\s*=\s*([^\n\r]{1,200})'
        matches = re.findall(pattern2, text, re.MULTILINE)
        for key, value in matches:
            specs[key.strip()] = value.strip()
        
        return specs