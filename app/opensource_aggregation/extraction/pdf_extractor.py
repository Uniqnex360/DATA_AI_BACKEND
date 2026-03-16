import httpx
import fitz  # PyMuPDF
import re
import logging
from typing import List, Optional
from io import BytesIO

from app.opensource_aggregation.models.schemas import (
    ExtractedAttribute, SourceResult, SourceType
)
from app.opensource_aggregation.config import config

logger = logging.getLogger("os_pdf_extractor")


class PdfExtractor:
    """Extract product attributes from PDF documents"""

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=config.download_timeout,
            follow_redirects=True
        )

    async def extract(self, url: str) -> SourceResult:
        """Download and extract attributes from a PDF"""
        try:
            response = await self.client.get(url)

            if response.status_code != 200:
                return SourceResult(
                    url=url,
                    source_type=SourceType.PDF_MANUAL,
                    success=False,
                    error=f"HTTP {response.status_code}"
                )

            content_type = response.headers.get("content-type", "")
            if "pdf" not in content_type.lower() and not url.lower().endswith(".pdf"):
                return SourceResult(
                    url=url,
                    source_type=SourceType.PDF_MANUAL,
                    success=False,
                    error="Not a PDF"
                )

            pdf_bytes = response.content
            attributes = self._extract_from_pdf(pdf_bytes, url)

            logger.info(f"📄 Extracted {len(attributes)} attributes from PDF: {url}")

            return SourceResult(
                url=url,
                source_type=SourceType.PDF_MANUAL,
                attributes=attributes,
                extraction_method="pdf",
                success=True
            )

        except Exception as e:
            logger.warning(f"PDF extraction failed for {url}: {e}")
            return SourceResult(
                url=url,
                source_type=SourceType.PDF_MANUAL,
                success=False,
                error=str(e)
            )

    def _extract_from_pdf(
        self, pdf_bytes: bytes, url: str
    ) -> List[ExtractedAttribute]:
        """Extract attributes from PDF content"""
        attributes = []
        confidence = config.source_confidence.get("pdf_manual", 0.95)

        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            max_pages = min(len(doc), config.max_pdf_pages)

            full_text = ""
            for page_num in range(max_pages):
                page = doc[page_num]
                full_text += page.get_text() + "\n"

            doc.close()

            # Method 1: Key-Value patterns
            attributes.extend(
                self._extract_key_value_pairs(full_text, url, confidence)
            )

            # Method 2: Table-like patterns
            attributes.extend(
                self._extract_table_patterns(full_text, url, confidence)
            )

        except Exception as e:
            logger.warning(f"PDF parsing error: {e}")

        return attributes

    def _extract_key_value_pairs(
        self, text: str, url: str, confidence: float
    ) -> List[ExtractedAttribute]:
        """Extract 'Key: Value' patterns from text"""
        attributes = []

        patterns = [
            # "Key: Value" or "Key - Value"
            r'^([A-Z][A-Za-z\s\.\-\/]+?)[\s]*[:–—-]\s*(.+?)$',
            # "Key .... Value" (tabular dots)
            r'^([A-Z][A-Za-z\s]+?)\s*\.{2,}\s*(.+?)$',
            # "Key    Value" (multiple spaces)
            r'^([A-Z][A-Za-z\s\.\-]+?)\s{3,}(.+?)$',
        ]

        for line in text.split('\n'):
            line = line.strip()
            if not line or len(line) < 5:
                continue

            for pattern in patterns:
                match = re.match(pattern, line)
                if match:
                    key = match.group(1).strip()
                    value = match.group(2).strip()

                    if (
                        3 < len(key) < 80
                        and 1 < len(value) < 300
                        and not key.isupper()  # Skip headings
                    ):
                        attributes.append(ExtractedAttribute(
                            name=key,
                            value=value,
                            confidence=confidence,
                            source_url=url
                        ))
                    break

        return attributes

    def _extract_table_patterns(
        self, text: str, url: str, confidence: float
    ) -> List[ExtractedAttribute]:
        """Extract table-structured data from PDF text"""
        attributes = []
        lines = text.split('\n')

        for i, line in enumerate(lines):
            line = line.strip()

            # Look for "Specification" section headers
            if re.match(
                r'(specifications?|technical data|properties|features)',
                line, re.IGNORECASE
            ):
                # Read next lines as potential key-value pairs
                for j in range(i + 1, min(i + 30, len(lines))):
                    next_line = lines[j].strip()
                    if not next_line:
                        continue

                    # Stop at next section header
                    if re.match(r'^[A-Z][A-Z\s]{5,}$', next_line):
                        break

                    # Try to split into key-value
                    parts = re.split(r'\s{2,}|\t', next_line, maxsplit=1)
                    if len(parts) == 2:
                        key, value = parts[0].strip(), parts[1].strip()
                        if 3 < len(key) < 80 and len(value) > 0:
                            attributes.append(ExtractedAttribute(
                                name=key,
                                value=value,
                                confidence=confidence * 0.9,
                                source_url=url
                            ))

        return attributes

    async def close(self):
        await self.client.aclose()