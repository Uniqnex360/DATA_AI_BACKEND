import logging
import re
import os
import httpx
from html import unescape
from typing import Optional, List
from urllib.parse import urljoin, urlparse
from app.sacred import extract_image_from_source
from app.aggregation.interfaces import IImageService
logger = logging.getLogger("image_service")
class ImageService(IImageService):
    def __init__(self, official_domains: list = None):
        self.official_domains = official_domains or []
        self.brand_domains = []  
    def _build_brand_domains(self, brand: str) -> List[str]:
        if not brand:
            return []
        domains = []
        brand_lower = brand.lower().strip()
        clean1 = re.sub(r'[^a-z0-9]', '', brand_lower)
        domains.append(clean1)
        clean2 = brand_lower.replace('.', '').replace(' ', '').replace('-', '').replace('_', '')
        if clean2 not in domains:
            domains.append(clean2)
        clean3 = brand_lower.replace(' ', '-').replace('.', '')
        if clean3 not in domains:
            domains.append(clean3)
        words = re.sub(r'[^a-z0-9\s]', '', brand_lower).split()
        if words:
            domains.append(words[-1])  
            if len(words) > 1:
                domains.append(words[0] + words[-1])  
        domains = [d for d in domains if d and len(d) > 2]
        return domains
    def is_official(self, url: str) -> bool:
        url_lower = url.lower()
        for domain in self.brand_domains:
            if domain in url_lower:
                return True
        return any(d in url_lower for d in self.official_domains)
    def clean_image_url(self, url: str) -> str:
        if not url:
            return ""
        url = unescape(url)
        url = url.strip()
        return url
    async def extract_best_image(
        self,
        sources: list,
        request_id: str,
        mpn: str = "",
        brand: str = "",
        source_urls: list = None,
    ) -> Optional[str]:
        
        if brand:
            self.brand_domains = self._build_brand_domains(brand)
            logger.info(f"[{request_id}]  Image search for {mpn} | Brand patterns: {self.brand_domains}")
        brand_image = None
        other_image = None
        sorted_sources = sorted(
            sources,
            key=lambda s: 100 if self.is_official(s.get("source_url", "")) else 0,
            reverse=True
        )
        for src in sorted_sources:
            url = src.get("source_url", "")
            try:
                html_text = src["raw_bytes"].decode('utf-8', errors='ignore')
            except Exception:
                continue
            try:
                is_brand = self.is_official(url)
                img = await extract_image_from_source(html_text, url, mpn)
                if img:
                    img = self.clean_image_url(img)  
                    if is_brand:
                        logger.info(f"[{request_id}]  Brand image found: {img}")
                        return img
                    elif not other_image:
                        other_image = img
                        logger.info(f"[{request_id}]  Non-brand image found: {img}")
            except Exception as e:
                logger.warning(f"[{request_id}] Image extraction failed for {url}: {e}")
        if other_image:
            logger.info(f"[{request_id}]  Using best available image: {other_image}")
            return other_image
        if source_urls:
            logger.info(f"[{request_id}]  Fallback: re-scraping source URLs...")
            img = await self._scrape_urls_for_image(source_urls, mpn, request_id)
            if img:
                return img
        logger.info(f"[{request_id}]  Fallback: trying Google Image search...")
        img = await self._google_image_search(mpn, brand, request_id)
        if img:
            return img
        logger.warning(f"[{request_id}]  No image found after all strategies")
        return None
    async def _scrape_urls_for_image(
        self, urls: List[str], mpn: str, request_id: str
    ) -> Optional[str]:
        sorted_urls = sorted(
            urls,
            key=lambda u: 100 if self.is_official(u) else 0,
            reverse=True
        )
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            for url in sorted_urls[:5]:
                try:
                    response = await client.get(url)
                    if response.status_code != 200:
                        continue
                    img = await extract_image_from_source(response.text, url, mpn)
                    if img:
                        img = self.clean_image_url(img)
                        logger.info(f"[{request_id}]  Fallback scrape image: {img}")
                        return img
                except Exception as e:
                    logger.debug(f"Fallback scrape failed for {url}: {e}")
                    continue
        return None
    async def _google_image_search(
        self, mpn: str, brand: str, request_id: str
    ) -> Optional[str]:
        api_key = os.getenv('SERPAPI_KEY')
        if not api_key:
            logger.debug("No SERPAPI_KEY, skipping Google image search")
            return None
        query = f"{brand} {mpn} product" if brand else f"{mpn} product"
        logger.info(f"[{request_id}] 🔍 Google Image search query: '{query}'") 
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    "https://serpapi.com/search",
                    params={
                        "engine": "google_images",
                        "q": query,
                        "api_key": api_key,
                        "num": 5
                    }
                )
                logger.info(f"[{request_id}] Google Image API status: {response.status_code}") 
                if response.status_code == 200:
                    data = response.json()
                    images = data.get('images_results', [])
                    logger.info(f"[{request_id}] Google returned {len(images)} images")
                    skip_patterns = [
                        'logo', 'icon', 'placeholder', 'avatar',
                        'facebook', 'twitter', 'instagram', 'youtube'
                    ]
                    for idx, img_data in enumerate(images, 1):
                        try:
                            img_url = img_data.get('original') or img_data.get('thumbnail')
                            if not img_url:
                                logger.debug(f"[{request_id}] Image {idx}: no URL field")
                                continue
                            
                            logger.debug(f"[{request_id}] Image {idx}: {img_url[:80]}")
                            if any(s in img_url.lower() for s in skip_patterns):
                                logger.debug(f"[{request_id}] Skipped (junk): {img_url[:60]}")
                                continue
                            img_url = self.clean_image_url(img_url)
                            logger.info(f"[{request_id}]  Google image found: {img_url[:100]}")
                            return img_url
                            
                        except Exception as img_err:
                            logger.warning(f"[{request_id}] Error processing image {idx}: {img_err}")
                            continue
                    logger.warning(f"[{request_id}] Google returned {len(images)} but all filtered/invalid")
                else:
                    error_text = response.text[:300] if hasattr(response, 'text') else str(response)
                    logger.error(f"[{request_id}] SERP API error {response.status_code}: {error_text}")
        except Exception as e:
            logger.error(f"[{request_id}] Google image search exception: {type(e).__name__}: {str(e)[:200]}")
            import traceback
            logger.debug(f"[{request_id}] Traceback: {traceback.format_exc()[:300]}")
        logger.warning(f"[{request_id}] Google Image search failed")
        return None
