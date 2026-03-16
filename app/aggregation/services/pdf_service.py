"""
Production-ready PDF extraction service
"""
import io
import logging
from typing import Optional, Dict, List
import PyPDF2
import pdfplumber
import re

logger = logging.getLogger("pdf_service")

class PDFExtractionService:
    """Extract text and structured data from PDFs"""
    
    def __init__(self, max_pages: int = 10):
        self.max_pages = max_pages
    
    async def extract_text(self, pdf_bytes: bytes) -> Optional[str]:
        """
        Extract text from PDF bytes using multiple strategies
        
        Args:
            pdf_bytes: Raw PDF bytes
            
        Returns:
            Extracted text or None if failed
        """
        try:
            # Strategy 1: Try PyPDF2 first (fast)
            text = self._extract_with_pypdf2(pdf_bytes)
            if text and len(text.strip()) > 100:
                logger.info(f"PyPDF2 extracted {len(text)} characters")
                return text
            
            # Strategy 2: Try pdfplumber (better for tables/complex layouts)
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
        """Extract text using PyPDF2"""
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
            logger.debug(f"PyPDF2 extraction failed: {e}")
            return None
    
    async def _extract_with_pdfplumber(self, pdf_bytes: bytes) -> Optional[str]:
        """Extract text using pdfplumber (better for tables)"""
        try:
            pdf_file = io.BytesIO(pdf_bytes)
            text_pages = []
            
            with pdfplumber.open(pdf_file) as pdf:
                for i, page in enumerate(pdf.pages[:self.max_pages]):
                    # Extract text
                    page_text = page.extract_text() or ""
                    
                    # Extract tables (spec sheets often in tables)
                    tables = page.extract_tables()
                    table_text = ""
                    for table in tables:
                        if table:
                            for row in table:
                                if row and any(cell for cell in row if cell):
                                    table_text += " | ".join([str(cell or "") for cell in row]) + "\n"
                    
                    # Combine text and tables
                    if page_text or table_text:
                        text_pages.append(f"--- Page {i+1} ---\n{page_text}\n{table_text}")
            
            return "\n\n".join(text_pages) if text_pages else None
            
        except Exception as e:
            logger.debug(f"pdfplumber extraction failed: {e}")
            return None
    
    def extract_specs_from_text(self, text: str) -> Dict[str, str]:
        """
        Extract key-value pairs from PDF text
        
        Looks for patterns like:
        - "Attribute: Value"
        - "Attribute = Value"
        - Tables with 2 columns
        """
        specs = {}
        
        # Pattern 1: "Attribute: Value" format
        pattern1 = r'([A-Za-z][A-Za-z\s\-]{2,50}):\s*([^\n\r]{1,200})'
        matches = re.findall(pattern1, text, re.MULTILINE)
        for key, value in matches:
            specs[key.strip()] = value.strip()
        
        # Pattern 2: "Attribute = Value" format
        pattern2 = r'([A-Za-z][A-Za-z\s\-]{2,50})\s*=\s*([^\n\r]{1,200})'
        matches = re.findall(pattern2, text, re.MULTILINE)
        for key, value in matches:
            specs[key.strip()] = value.strip()
        
        return specs