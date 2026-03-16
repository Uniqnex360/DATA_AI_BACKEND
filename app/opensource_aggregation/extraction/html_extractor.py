# import httpx
# import logging
# import re
# from typing import List, Optional, Dict
# from bs4 import BeautifulSoup
# from urllib.parse import urljoin, urlparse

# from app.opensource_aggregation.models.schemas import (
#     ExtractedAttribute, SourceResult, SourceType
# )
# from app.opensource_aggregation.config import config

# logger = logging.getLogger("os_html_extractor")


# class HtmlExtractor:
#     """Extract product attributes from HTML pages usin    g BeautifulSoup"""

#     def __init__(self):
#         self.client = httpx.AsyncClient(
#             timeout=config.download_timeout,
#             follow_redirects=True,
#             headers={
#                 "User-Agent": "Mozilla/5.0 (compatible; ProductBot/1.0)"
#             }
#         )

#     async def extract(self, url: str, source_type: SourceType) -> SourceResult:
#         """Extract attributes from a URL"""
#         try:
#             response = await self.client.get(url)

#             if response.status_code != 200:
#                 return SourceResult(
#                     url=url,
#                     source_type=source_type,
#                     success=False,
#                     error=f"HTTP {response.status_code}"
#                 )

#             html = response.text

#             if len(html) > config.max_html_size:
#                 html = html[:config.max_html_size]
        
#             soup = BeautifulSoup(html, "html.parser") 
#             confidence = config.source_confidence.get(source_type.value, 0.5)

#             # Extract from multiple methods
#             attributes = []
#             attributes.extend(self._extract_from_tables(soup, url, confidence))
#             attributes.extend(self._extract_from_spec_lists(soup, url, confidence))
#             attributes.extend(self._extract_from_dl_tags(soup, url, confidence))
#             attributes.extend(self._extract_from_meta_tags(soup, url, confidence))
#             attributes.extend(self._extract_from_json_ld(soup, url, confidence))

#             # Extract image
#             image_url = self._extract_image(soup, url)

#             logger.info(f"✅ Extracted {len(attributes)} attributes from {url}")

#             return SourceResult(
#                 url=url,
#                 source_type=source_type,
#                 attributes=attributes,
#                 image_url=image_url,
#                 success=True
#             )

#         except Exception as e:
#             logger.warning(f"Extraction failed for {url}: {e}")
#             return SourceResult(
#                 url=url,
#                 source_type=source_type,
#                 success=False,
#                 error=str(e)
#             )

#     def _extract_from_tables(
#         self, soup: BeautifulSoup, url: str, confidence: float
#     ) -> List[ExtractedAttribute]:
#         """Extract from HTML tables (most common spec format)"""
#         attributes = []

#         tables = soup.find_all("table")
#         for table in tables:
#             rows = table.find_all("tr")
#             for row in rows:
#                 cols = row.find_all(["td", "th"])

#                 if len(cols) == 2:
#                     key = cols[0].get_text(strip=True)
#                     value = cols[1].get_text(strip=True)

#                     if key and value and len(key) < 100 and len(value) < 500:
#                         # Skip navigation/junk rows
#                         if self._is_valid_attribute(key, value):
#                             attributes.append(ExtractedAttribute(
#                                 name=key,
#                                 value=value,
#                                 confidence=confidence,
#                                 source_url=url
#                             ))

#         return attributes

#     def _extract_from_spec_lists(
#         self, soup: BeautifulSoup, url: str, confidence: float
#     ) -> List[ExtractedAttribute]:
#         """Extract from specification lists (ul/li with key: value pattern)"""
#         attributes = []

#         # Look for spec containers
#         spec_containers = soup.find_all(
#             ["div", "section", "ul"],
#             class_=re.compile(
#                 r"spec|feature|detail|attribute|property|param",
#                 re.IGNORECASE
#             )
#         )

#         for container in spec_containers:
#             items = container.find_all("li")
#             for item in items:
#                 text = item.get_text(strip=True)

#                 # Try "Key: Value" pattern
#                 if ":" in text:
#                     parts = text.split(":", 1)
#                     key = parts[0].strip()
#                     value = parts[1].strip()

#                     if key and value and len(key) < 100:
#                         attributes.append(ExtractedAttribute(
#                             name=key,
#                             value=value,
#                             confidence=confidence * 0.9,  # Slightly less confidence
#                             source_url=url
#                         ))

#                 # Try span-based key-value
#                 spans = item.find_all("span")
#                 if len(spans) == 2:
#                     key = spans[0].get_text(strip=True)
#                     value = spans[1].get_text(strip=True)
#                     if key and value:
#                         attributes.append(ExtractedAttribute(
#                             name=key,
#                             value=value,
#                             confidence=confidence * 0.9,
#                             source_url=url
#                         ))

#         return attributes

#     def _extract_from_dl_tags(
#         self, soup: BeautifulSoup, url: str, confidence: float
#     ) -> List[ExtractedAttribute]:
#         """Extract from definition lists (dl/dt/dd)"""
#         attributes = []

#         for dl in soup.find_all("dl"):
#             terms = dl.find_all("dt")
#             values = dl.find_all("dd")

#             for dt, dd in zip(terms, values):
#                 key = dt.get_text(strip=True)
#                 value = dd.get_text(strip=True)

#                 if key and value and len(key) < 100:
#                     attributes.append(ExtractedAttribute(
#                         name=key,
#                         value=value,
#                         confidence=confidence,
#                         source_url=url
#                     ))

#         return attributes

#     def _extract_from_meta_tags(
#         self, soup: BeautifulSoup, url: str, confidence: float
#     ) -> List[ExtractedAttribute]:
#         """Extract from meta tags and Open Graph"""
#         attributes = []

#         # Product-specific meta tags
#         meta_mappings = {
#             'product:price:amount': 'Price',
#             'product:price:currency': 'Currency',
#             'og:title': 'Title',
#             'og:description': 'Description',
#         }

#         for property_name, attr_name in meta_mappings.items():
#             meta = soup.find("meta", property=property_name)
#             if meta and meta.get("content"):
#                 attributes.append(ExtractedAttribute(
#                     name=attr_name,
#                     value=meta["content"],
#                     confidence=confidence * 0.8,
#                     source_url=url
#                 ))

#         return attributes

#     def _extract_from_json_ld(
#         self, soup: BeautifulSoup, url: str, confidence: float
#     ) -> List[ExtractedAttribute]:
#         """Extract from JSON-LD structured data"""
#         import json
#         attributes = []

#         scripts = soup.find_all("script", type="application/ld+json")
#         for script in scripts:
#             try:
#                 data = json.loads(script.string)

#                 if isinstance(data, list):
#                     data = data[0]

#                 if data.get("@type") == "Product":
#                     field_mappings = {
#                         "name": "Product Name",
#                         "brand": "Brand",
#                         "sku": "SKU",
#                         "mpn": "MPN",
#                         "weight": "Weight",
#                         "color": "Color",
#                         "material": "Material",
#                         "description": "Description"
#                     }

#                     for json_key, attr_name in field_mappings.items():
#                         value = data.get(json_key)
#                         if value:
#                             if isinstance(value, dict):
#                                 value = value.get("name", str(value))
#                             attributes.append(ExtractedAttribute(
#                                 name=attr_name,
#                                 value=str(value),
#                                 confidence=confidence * 0.95,
#                                 source_url=url
#                             ))

#                     # Extract additionalProperty
#                     for prop in data.get("additionalProperty", []):
#                         if prop.get("name") and prop.get("value"):
#                             attributes.append(ExtractedAttribute(
#                                 name=prop["name"],
#                                 value=str(prop["value"]),
#                                 confidence=confidence,
#                                 source_url=url
#                             ))

#             except (json.JSONDecodeError, AttributeError):
#                 continue

#         return attributes

#     def _extract_image(self, soup: BeautifulSoup, base_url: str) -> Optional[str]:
#         """Extract main product image"""
#         # Try Open Graph
#         og = soup.find("meta", property="og:image")
#         if og and og.get("content"):
#             return urljoin(base_url, og["content"])

#         # Try JSON-LD
#         import json
#         for script in soup.find_all("script", type="application/ld+json"):
#             try:
#                 data = json.loads(script.string)
#                 if isinstance(data, list):
#                     data = data[0]
#                 if data.get("image"):
#                     img = data["image"]
#                     if isinstance(img, list):
#                         img = img[0]
#                     if isinstance(img, dict):
#                         img = img.get("url", "")
#                     return urljoin(base_url, img)
#             except:
#                 continue

#         # Try large images
#         for img in soup.find_all("img"):
#             src = img.get("src", "")
#             alt = img.get("alt", "").lower()
#             width = img.get("width")

#             if any(k in src.lower() for k in ["product", "main", "hero"]):
#                 return urljoin(base_url, src)

#             try:
#                 if width and int(width) > 300:
#                     return urljoin(base_url, src)
#             except:
#                 continue

#         return None

#     def _is_valid_attribute(self, key: str, value: str) -> bool:
#         """Filter out junk attributes"""
#         junk_keys = [
#             "add to cart", "buy now", "share", "tweet", "pin",
#             "quantity", "qty", "review", "rating", "price",
#             "in stock", "availability", "shipping"
#         ]

#         key_lower = key.lower()
#         if any(junk in key_lower for junk in junk_keys):
#             return False

#         if len(key) < 2 or len(value) < 1:
#             return False

#         if value.startswith("http") and "image" not in key_lower:
#             return False

#         return True

#     async def close(self):
#         await self.client.aclose()

import httpx
import logging
import re
import json
from typing import List, Optional, Dict
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

from app.opensource_aggregation.models.schemas import (
    ExtractedAttribute, SourceResult, SourceType
)
from app.opensource_aggregation.config import config

logger = logging.getLogger("os_html_extractor")


class HtmlExtractor:
    """Extract product attributes from HTML pages using BeautifulSoup"""

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=config.download_timeout,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; ProductBot/1.0)"
            }
        )

    async def extract(self, url: str, source_type: SourceType, mpn: Optional[str] = None) -> SourceResult:
        """
        Extract attributes from a URL.
        If mpn is provided, the page is checked for relevance.
        """
        try:
            response = await self.client.get(url)

            if response.status_code != 200:
                return SourceResult(
                    url=url,
                    source_type=source_type,
                    success=False,
                    error=f"HTTP {response.status_code}"
                )

            html = response.text

            # Quick relevance check: if MPN is given, it must appear in the page content
            if mpn and mpn.lower() not in html.lower():
                logger.info(f"⏭️ Skipping {url} – MPN '{mpn}' not found in page content")
                return SourceResult(
                    url=url,
                    source_type=source_type,
                    success=False,
                    error="MPN not found in page content"
                )

            if len(html) > config.max_html_size:
                html = html[:config.max_html_size]

            soup = BeautifulSoup(html, "html.parser")
            confidence = config.source_confidence.get(source_type.value, 0.5)

            attributes = []
            attributes.extend(self._extract_from_tables(soup, url, confidence))
            attributes.extend(self._extract_from_spec_lists(soup, url, confidence))
            attributes.extend(self._extract_from_dl_tags(soup, url, confidence))
            attributes.extend(self._extract_from_meta_tags(soup, url, confidence))
            attributes.extend(self._extract_from_json_ld(soup, url, confidence))
            attributes.extend(self._extract_from_microdata(soup, url, confidence))

            # If no attributes found, optionally retry with Playwright
            if not attributes and getattr(config, 'use_playwright_fallback', False):
                logger.info(f"🔄 Retrying {url} with Playwright")
                playwright_result = await self._extract_with_playwright(url, source_type, mpn)
                if playwright_result.success:
                    return playwright_result

            image_url = self._extract_image(soup, url)

            logger.info(f"✅ Extracted {len(attributes)} attributes from {url}")

            return SourceResult(
                url=url,
                source_type=source_type,
                attributes=attributes,
                image_url=image_url,
                success=True
            )

        except Exception as e:
            logger.warning(f"Extraction failed for {url}: {e}")
            return SourceResult(
                url=url,
                source_type=source_type,
                success=False,
                error=str(e)
            )

    # ------------------------------------------------------------------
    # Existing extraction methods (unchanged)
    # ------------------------------------------------------------------

    def _extract_from_tables(
        self, soup: BeautifulSoup, url: str, confidence: float
    ) -> List[ExtractedAttribute]:
        """Extract from HTML tables (most common spec format)"""
        attributes = []

        tables = soup.find_all("table")
        for table in tables:
            rows = table.find_all("tr")
            for row in rows:
                cols = row.find_all(["td", "th"])

                if len(cols) == 2:
                    key = cols[0].get_text(strip=True)
                    value = cols[1].get_text(strip=True)

                    if key and value and len(key) < 100 and len(value) < 500:
                        if self._is_valid_attribute(key, value):
                            attributes.append(ExtractedAttribute(
                                name=key,
                                value=value,
                                confidence=confidence,
                                source_url=url
                            ))

        return attributes

    def _extract_from_spec_lists(
        self, soup: BeautifulSoup, url: str, confidence: float
    ) -> List[ExtractedAttribute]:
        """Extract from specification lists (ul/li with key: value pattern)"""
        attributes = []

        spec_containers = soup.find_all(
            ["div", "section", "ul"],
            class_=re.compile(
                r"spec|feature|detail|attribute|property|param",
                re.IGNORECASE
            )
        )

        for container in spec_containers:
            items = container.find_all("li")
            for item in items:
                text = item.get_text(strip=True)

                if ":" in text:
                    parts = text.split(":", 1)
                    key = parts[0].strip()
                    value = parts[1].strip()
                    if key and value and len(key) < 100:
                        attributes.append(ExtractedAttribute(
                            name=key,
                            value=value,
                            confidence=confidence * 0.9,
                            source_url=url
                        ))

                spans = item.find_all("span")
                if len(spans) == 2:
                    key = spans[0].get_text(strip=True)
                    value = spans[1].get_text(strip=True)
                    if key and value:
                        attributes.append(ExtractedAttribute(
                            name=key,
                            value=value,
                            confidence=confidence * 0.9,
                            source_url=url
                        ))

        return attributes

    def _extract_from_dl_tags(
        self, soup: BeautifulSoup, url: str, confidence: float
    ) -> List[ExtractedAttribute]:
        """Extract from definition lists (dl/dt/dd)"""
        attributes = []

        for dl in soup.find_all("dl"):
            terms = dl.find_all("dt")
            values = dl.find_all("dd")

            for dt, dd in zip(terms, values):
                key = dt.get_text(strip=True)
                value = dd.get_text(strip=True)

                if key and value and len(key) < 100:
                    attributes.append(ExtractedAttribute(
                        name=key,
                        value=value,
                        confidence=confidence,
                        source_url=url
                    ))

        return attributes

    def _extract_from_meta_tags(
        self, soup: BeautifulSoup, url: str, confidence: float
    ) -> List[ExtractedAttribute]:
        """Extract from meta tags and Open Graph"""
        attributes = []

        meta_mappings = {
            'product:price:amount': 'Price',
            'product:price:currency': 'Currency',
            'og:title': 'Title',
            'og:description': 'Description',
        }

        for property_name, attr_name in meta_mappings.items():
            meta = soup.find("meta", property=property_name)
            if meta and meta.get("content"):
                attributes.append(ExtractedAttribute(
                    name=attr_name,
                    value=meta["content"],
                    confidence=confidence * 0.8,
                    source_url=url
                ))

        return attributes

    # ------------------------------------------------------------------
    # Enhanced JSON‑LD extraction
    # ------------------------------------------------------------------

    def _extract_from_json_ld(
        self, soup: BeautifulSoup, url: str, confidence: float
    ) -> List[ExtractedAttribute]:
        """Extract from JSON-LD structured data, traversing @graph and lists."""
        attributes = []
        scripts = soup.find_all("script", type="application/ld+json")
        for script in scripts:
            try:
                data = json.loads(script.string)
                self._traverse_json_ld(data, url, confidence, attributes)
            except (json.JSONDecodeError, AttributeError):
                continue
        return attributes

    def _traverse_json_ld(self, data, url: str, confidence: float, attributes: List[ExtractedAttribute]) -> None:
        """Recursively traverse JSON‑LD to find Product nodes."""
        if isinstance(data, dict):
            if data.get("@type") == "Product":
                self._extract_product_from_json(data, url, confidence, attributes)
            # Look inside @graph
            if "@graph" in data:
                for item in data["@graph"]:
                    self._traverse_json_ld(item, url, confidence, attributes)
            # Also search nested properties
            for value in data.values():
                self._traverse_json_ld(value, url, confidence, attributes)
        elif isinstance(data, list):
            for item in data:
                self._traverse_json_ld(item, url, confidence, attributes)

    def _extract_product_from_json(self, data: dict, url: str, confidence: float, attributes: List[ExtractedAttribute]) -> None:
        """Extract attributes from a single Product JSON node."""
        field_mappings = {
            "name": "Product Name",
            "brand": "Brand",
            "sku": "SKU",
            "mpn": "MPN",
            "weight": "Weight",
            "color": "Color",
            "material": "Material",
            "description": "Description"
        }

        for json_key, attr_name in field_mappings.items():
            value = data.get(json_key)
            if value:
                if isinstance(value, dict):
                    value = value.get("name", str(value))
                attributes.append(ExtractedAttribute(
                    name=attr_name,
                    value=str(value),
                    confidence=confidence * 0.95,
                    source_url=url
                ))

        # Extract additionalProperty
        for prop in data.get("additionalProperty", []):
            if prop.get("name") and prop.get("value"):
                attributes.append(ExtractedAttribute(
                    name=prop["name"],
                    value=str(prop["value"]),
                    confidence=confidence,
                    source_url=url
                ))

    # ------------------------------------------------------------------
    # Microdata extraction
    # ------------------------------------------------------------------

    def _extract_from_microdata(
        self, soup: BeautifulSoup, url: str, confidence: float
    ) -> List[ExtractedAttribute]:
        """Extract attributes from HTML5 microdata (itemprop)."""
        attributes = []
        for tag in soup.find_all(attrs={"itemprop": True}):
            name = tag.get("itemprop")
            # Prefer content attribute for meta/link, else text
            if tag.name in ["meta", "link"]:
                value = tag.get("content") or tag.get("href")
            else:
                value = tag.get_text(strip=True)
            if name and value:
                attributes.append(ExtractedAttribute(
                    name=name,
                    value=value,
                    confidence=confidence * 0.9,
                    source_url=url
                ))
        return attributes

    # ------------------------------------------------------------------
    # Image extraction
    # ------------------------------------------------------------------

    def _extract_image(self, soup: BeautifulSoup, base_url: str) -> Optional[str]:
        """Extract main product image."""
        # Try Open Graph
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            return urljoin(base_url, og["content"])

        # Try JSON‑LD (via image property)
        scripts = soup.find_all("script", type="application/ld+json")
        for script in scripts:
            try:
                data = json.loads(script.string)
                if isinstance(data, list):
                    data = data[0] if data else {}
                if data.get("image"):
                    img = data["image"]
                    if isinstance(img, list):
                        img = img[0]
                    if isinstance(img, dict):
                        img = img.get("url", "")
                    return urljoin(base_url, img)
            except:
                continue

        # Try large images with product‑related keywords
        for img in soup.find_all("img"):
            src = img.get("src", "")
            alt = img.get("alt", "").lower()
            width = img.get("width")

            if any(k in src.lower() for k in ["product", "main", "hero"]):
                return urljoin(base_url, src)

            try:
                if width and int(width) > 300:
                    return urljoin(base_url, src)
            except:
                continue

        return None

    # ------------------------------------------------------------------
    # Playwright fallback (optional)
    # ------------------------------------------------------------------

    async def _extract_with_playwright(self, url: str, source_type: SourceType, mpn: Optional[str] = None) -> SourceResult:
        """Fallback extraction using Playwright for JavaScript‑rendered pages."""
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(url, wait_until="networkidle")
                html = await page.content()
                await browser.close()

            if mpn and mpn.lower() not in html.lower():
                return SourceResult(
                    url=url,
                    source_type=source_type,
                    success=False,
                    error="MPN not found after Playwright"
                )

            soup = BeautifulSoup(html, "html.parser")
            confidence = config.source_confidence.get(source_type.value, 0.5)

            attributes = []
            attributes.extend(self._extract_from_tables(soup, url, confidence))
            attributes.extend(self._extract_from_spec_lists(soup, url, confidence))
            attributes.extend(self._extract_from_dl_tags(soup, url, confidence))
            attributes.extend(self._extract_from_meta_tags(soup, url, confidence))
            attributes.extend(self._extract_from_json_ld(soup, url, confidence))
            attributes.extend(self._extract_from_microdata(soup, url, confidence))

            image_url = self._extract_image(soup, url)

            return SourceResult(
                url=url,
                source_type=source_type,
                attributes=attributes,
                image_url=image_url,
                success=True
            )
        except Exception as e:
            logger.warning(f"Playwright extraction failed for {url}: {e}")
            return SourceResult(
                url=url,
                source_type=source_type,
                success=False,
                error=str(e)
            )

    # ------------------------------------------------------------------
    # Helper: junk filter
    # ------------------------------------------------------------------

    def _is_valid_attribute(self, key: str, value: str) -> bool:
        """Filter out junk attributes."""
        junk_keys = [
            "add to cart", "buy now", "share", "tweet", "pin",
            "quantity", "qty", "review", "rating", "price",
            "in stock", "availability", "shipping"
        ]

        key_lower = key.lower()
        if any(junk in key_lower for junk in junk_keys):
            return False

        if len(key) < 2 or len(value) < 1:
            return False

        if value.startswith("http") and "image" not in key_lower:
            return False

        return True

    async def close(self):
        await self.client.aclose()