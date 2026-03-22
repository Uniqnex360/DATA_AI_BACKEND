# import logging
# import re
# import os
# import httpx
# from html import unescape
from typing import Optional, List,Dict
# from urllib.parse import urljoin, urlparse
# from app.sacred import extract_image_from_source
# from app.aggregation.interfaces import IImageService
# logger = logging.getLogger("image_service")
# class ImageService(IImageService):
#     def __init__(self, official_domains: list = None):
#         self.official_domains = official_domains or []
#         self.brand_domains = []  
#     def _build_brand_domains(self, brand: str) -> List[str]:
#         if not brand:
#             return []
#         domains = []
#         brand_lower = brand.lower().strip()
#         clean1 = re.sub(r'[^a-z0-9]', '', brand_lower)
#         domains.append(clean1)
#         clean2 = brand_lower.replace('.', '').replace(' ', '').replace('-', '').replace('_', '')
#         if clean2 not in domains:
#             domains.append(clean2)
#         clean3 = brand_lower.replace(' ', '-').replace('.', '')
#         if clean3 not in domains:
#             domains.append(clean3)
#         words = re.sub(r'[^a-z0-9\s]', '', brand_lower).split()
#         if words:
#             domains.append(words[-1])  
#             if len(words) > 1:
#                 domains.append(words[0] + words[-1])  
#         domains = [d for d in domains if d and len(d) > 2]
#         return domains
#     def is_official(self, url: str) -> bool:
#         url_lower = url.lower()
#         for domain in self.brand_domains:
#             if domain in url_lower:
#                 return True
#         return any(d in url_lower for d in self.official_domains)
#     def clean_image_url(self, url: str) -> str:
#         if not url:
#             return ""
#         url = unescape(url)
#         url = url.strip()
#         return url
#     async def extract_best_image(
#         self,
#         sources: list,
#         request_id: str,
#         mpn: str = "",
#         brand: str = "",
#         source_urls: list = None,
#     ) -> Optional[str]:
        
#         if brand:
#             self.brand_domains = self._build_brand_domains(brand)
#             logger.info(f"[{request_id}]  Image search for {mpn} | Brand patterns: {self.brand_domains}")
#         brand_image = None
#         other_image = None
#         sorted_sources = sorted(
#             sources,
#             key=lambda s: 100 if self.is_official(s.get("source_url", "")) else 0,
#             reverse=True
#         )
#         for src in sorted_sources:
#             url = src.get("source_url", "")
#             try:
#                 html_text = src["raw_bytes"].decode('utf-8', errors='ignore')
#             except Exception:
#                 continue
#             try:
#                 is_brand = self.is_official(url)
#                 img = await extract_image_from_source(html_text, url, mpn)
#                 if img:
#                     img = self.clean_image_url(img)  
#                     if is_brand:
#                         logger.info(f"[{request_id}]  Brand image found: {img}")
#                         return img
#                     elif not other_image:
#                         other_image = img
#                         logger.info(f"[{request_id}]  Non-brand image found: {img}")
#             except Exception as e:
#                 logger.warning(f"[{request_id}] Image extraction failed for {url}: {e}")
#         if other_image:
#             logger.info(f"[{request_id}]  Using best available image: {other_image}")
#             return other_image
#         if source_urls:
#             logger.info(f"[{request_id}]  Fallback: re-scraping source URLs...")
#             img = await self._scrape_urls_for_image(source_urls, mpn, request_id)
#             if img:
#                 return img
#         logger.info(f"[{request_id}]  Fallback: trying Google Image search...")
#         img = await self._google_image_search(mpn, brand, request_id)
#         if img:
#             return img
#         logger.warning(f"[{request_id}]  No image found after all strategies")
#         return None
#     async def _scrape_urls_for_image(
#         self, urls: List[str], mpn: str, request_id: str
#     ) -> Optional[str]:
#         sorted_urls = sorted(
#             urls,
#             key=lambda u: 100 if self.is_official(u) else 0,
#             reverse=True
#         )
#         async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
#             for url in sorted_urls[:5]:
#                 try:
#                     response = await client.get(url)
#                     if response.status_code != 200:
#                         continue
#                     img = await extract_image_from_source(response.text, url, mpn)
#                     if img:
#                         img = self.clean_image_url(img)
#                         logger.info(f"[{request_id}]  Fallback scrape image: {img}")
#                         return img
#                 except Exception as e:
#                     logger.debug(f"Fallback scrape failed for {url}: {e}")
#                     continue
#         return None
#     async def _google_image_search(
#         self, mpn: str, brand: str, request_id: str
#     ) -> Optional[str]:
#         api_key = os.getenv('SERPAPI_KEY')
#         if not api_key:
#             logger.debug("No SERPAPI_KEY, skipping Google image search")
#             return None
#         query = f"{brand} {mpn} product" if brand else f"{mpn} product"
#         logger.info(f"[{request_id}] 🔍 Google Image search query: '{query}'") 
#         try:
#             async with httpx.AsyncClient(timeout=15.0) as client:
#                 response = await client.get(
#                     "https://serpapi.com/search",
#                     params={
#                         "engine": "google_images",
#                         "q": query,
#                         "api_key": api_key,
#                         "num": 5
#                     }
#                 )
#                 logger.info(f"[{request_id}] Google Image API status: {response.status_code}") 
#                 if response.status_code == 200:
#                     data = response.json()
#                     images = data.get('images_results', [])
#                     logger.info(f"[{request_id}] Google returned {len(images)} images")
#                     skip_patterns = [
#                         'logo', 'icon', 'placeholder', 'avatar',
#                         'facebook', 'twitter', 'instagram', 'youtube'
#                     ]
#                     for idx, img_data in enumerate(images, 1):
#                         try:
#                             img_url = img_data.get('original') or img_data.get('thumbnail')
#                             if not img_url:
#                                 logger.debug(f"[{request_id}] Image {idx}: no URL field")
#                                 continue
                            
#                             logger.debug(f"[{request_id}] Image {idx}: {img_url[:80]}")
#                             if any(s in img_url.lower() for s in skip_patterns):
#                                 logger.debug(f"[{request_id}] Skipped (junk): {img_url[:60]}")
#                                 continue
#                             img_url = self.clean_image_url(img_url)
#                             logger.info(f"[{request_id}]  Google image found: {img_url[:100]}")
#                             return img_url
                            
#                         except Exception as img_err:
#                             logger.warning(f"[{request_id}] Error processing image {idx}: {img_err}")
#                             continue
#                     logger.warning(f"[{request_id}] Google returned {len(images)} but all filtered/invalid")
#                 else:
#                     error_text = response.text[:300] if hasattr(response, 'text') else str(response)
#                     logger.error(f"[{request_id}] SERP API error {response.status_code}: {error_text}")
#         except Exception as e:
#             logger.error(f"[{request_id}] Google image search exception: {type(e).__name__}: {str(e)[:200]}")
#             import traceback
#             logger.debug(f"[{request_id}] Traceback: {traceback.format_exc()[:300]}")
#         logger.warning(f"[{request_id}] Google Image search failed")
#         return None
import re
from typing import Optional
from urllib.parse import urljoin, urlparse
import logging

logger = logging.getLogger('image_service')

async def extract_best_image(html_content: str, base_url: str, mpn: str) -> Optional[str]:
    """
    Extract the best product image from HTML with intelligent fallbacks.
    
    Args:
        html_content: Raw HTML content
        base_url: Base URL for resolving relative paths
        mpn: Product MPN for validation
        
    Returns:
        Complete image URL with extension, or None
    """
    
    # Priority 1: Meta tags (most reliable)
    meta_patterns = [
        r'<meta\s+property="og:image"\s+content="([^"]+)"',
        r'<meta\s+name="twitter:image"\s+content="([^"]+)"',
        r'<meta\s+property="og:image:secure_url"\s+content="([^"]+)"'
    ]
    
    for pattern in meta_patterns:
        match = re.search(pattern, html_content, re.IGNORECASE)
        if match:
            img_url = match.group(1)
            complete_url = ensure_complete_image_url(img_url, base_url, html_content)
            if complete_url and is_valid_product_image(complete_url, mpn):
                logger.info(f"✓ Meta tag image found: {complete_url[:80]}")
                return complete_url
    
    # Priority 2: High-quality img tags
    img_patterns = [
        # Data attributes (lazy loading)
        r'<img[^>]+data-zoom-image="([^"]+)"',
        r'<img[^>]+data-large="([^"]+)"',
        r'<img[^>]+data-src="([^"]+)"',
        
        # Class/ID based
        r'<img[^>]+class="[^"]*product[^"]*"[^>]+src="([^"]+)"',
        r'<img[^>]+id="[^"]*main[^"]*"[^>]+src="([^"]+)"',
        
        # MPN in URL
        rf'<img[^>]+src="([^"]*{re.escape(mpn.lower())}[^"]*)"',
        
        # Size indicators
        r'<img[^>]+src="([^"]*1200[^"]*)"',
        r'<img[^>]+src="([^"]*large[^"]*)"',
        r'<img[^>]+src="([^"]*zoom[^"]*)"'
    ]
    
    for pattern in img_patterns:
        matches = re.findall(pattern, html_content, re.IGNORECASE)
        for img_url in matches:
            complete_url = ensure_complete_image_url(img_url, base_url, html_content)
            if complete_url and is_valid_product_image(complete_url, mpn):
                logger.info(f"✓ Image tag found: {complete_url[:80]}")
                return complete_url
    
    # Priority 3: Any reasonable product image
    generic_pattern = r'<img[^>]+src="([^"]+)"'
    matches = re.findall(generic_pattern, html_content, re.IGNORECASE)
    
    # Score and rank images
    scored_images = []
    for img_url in matches:
        complete_url = ensure_complete_image_url(img_url, base_url, html_content)
        if complete_url:
            score = score_image_url(complete_url, mpn)
            if score > 0:
                scored_images.append((score, complete_url))
    
    if scored_images:
        scored_images.sort(reverse=True)
        best_url = scored_images[0][1]
        logger.info(f"✓ Scored image (score={scored_images[0][0]}): {best_url[:80]}")
        return best_url
    
    logger.warning("✗ No valid product image found")
    return None


def ensure_complete_image_url(img_url: str, base_url: str, html_content: str) -> Optional[str]:
    """
    Ensure image URL is complete with proper extension.
    
    Handles cases like:
    - Relative URLs → Convert to absolute
    - Missing extensions → Try to find complete URL in HTML
    - Protocol-relative URLs → Add https:
    """
    if not img_url:
        return None
    
    # Skip data URIs, SVGs, placeholders
    if any(x in img_url.lower() for x in ['data:image', '.svg', 'placeholder', 'blank.', 'spacer.']):
        return None
    
    # Make absolute
    if img_url.startswith('//'):
        img_url = 'https:' + img_url
    elif not img_url.startswith('http'):
        img_url = urljoin(base_url, img_url)
    
    # Check if it has a valid extension
    valid_extensions = ['.jpg', '.jpeg', '.png', '.webp', '.gif']
    if any(img_url.lower().endswith(ext) for ext in valid_extensions):
        return img_url
    
    # Try to find complete URL in HTML
    base_name = img_url.split('/')[-1].split('?')[0]
    for ext in ['.jpg', '.jpeg', '.png', '.webp']:
        pattern = rf'{re.escape(base_name)}{ext}'
        if re.search(pattern, html_content, re.IGNORECASE):
            complete_url = img_url + ext
            logger.info(f"✓ Completed URL: {img_url} → {complete_url}")
            return complete_url
    
    # Last resort: try .jpg
    logger.warning(f"⚠ Guessing extension for: {img_url}")
    return img_url + '.jpg'


def is_valid_product_image(url: str, mpn: str) -> bool:
    """
    Validate if URL is likely a product image.
    """
    url_lower = url.lower()
    
    # Exclude common non-product images
    exclude_terms = [
        'logo', 'icon', 'badge', 'banner', 'social',
        'facebook', 'twitter', 'instagram',
        'thumbnail', 'thumb', '50x50', '100x100',
        'avatar', 'profile', 'author'
    ]
    
    if any(term in url_lower for term in exclude_terms):
        return False
    
    # Must have valid extension
    if not any(url_lower.endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp', '.gif']):
        return False
    
    # Prefer URLs with product indicators
    return True


def score_image_url(url: str, mpn: str) -> int:
    """
    Score image URL quality (higher = better).
    """
    score = 0
    url_lower = url.lower()
    mpn_lower = mpn.lower()
    
    # Bonus for MPN in URL
    if mpn_lower in url_lower:
        score += 100
    
    # Bonus for quality indicators
    if any(x in url_lower for x in ['1200', '1500', '2000', 'large', 'zoom', 'hires', 'original']):
        score += 50
    
    # Bonus for product indicators
    if any(x in url_lower for x in ['product', 'main', 'hero', 'primary']):
        score += 30
    
    # Penalty for small sizes
    if any(x in url_lower for x in ['thumb', 'small', '100', '150', '200']):
        score -= 50
    
    # Penalty for non-product terms
    if any(x in url_lower for x in ['logo', 'icon', 'badge', 'social']):
        score -= 100
    
    return score
def extract_best_image_fallback(all_extractions: List[Dict]) -> Optional[str]:
    """Extract best image from all sources."""
    for source in all_extractions:
        img_url = source.get('image_url')
        if img_url and isinstance(img_url, str) and img_url.strip():
            if img_url.startswith('http'):
                logger.info(f"Using image from {source['url']}: {img_url[:80]}")
                return img_url.strip()
    logger.warning("No valid image URL found in any source")
    return None