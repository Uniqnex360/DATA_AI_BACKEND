from typing import Optional, List,Dict
import re
from typing import Optional
from urllib.parse import urljoin, urlparse
import logging
logger = logging.getLogger('image_service')
async def extract_best_image(html_content: str, base_url: str, mpn: str) -> Optional[str]:
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
    img_patterns = [
        r'<img[^>]+data-zoom-image="([^"]+)"',
        r'<img[^>]+data-large="([^"]+)"',
        r'<img[^>]+data-src="([^"]+)"',
        r'<img[^>]+class="[^"]*product[^"]*"[^>]+src="([^"]+)"',
        r'<img[^>]+id="[^"]*main[^"]*"[^>]+src="([^"]+)"',
        rf'<img[^>]+src="([^"]*{re.escape(mpn.lower())}[^"]*)"',
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
    generic_pattern = r'<img[^>]+src="([^"]+)"'
    matches = re.findall(generic_pattern, html_content, re.IGNORECASE)
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
    if not img_url:
        return None
    if any(x in img_url.lower() for x in ['data:image', '.svg', 'placeholder', 'blank.', 'spacer.']):
        return None
    if img_url.startswith('//'):
        img_url = 'https:' + img_url
    elif not img_url.startswith('http'):
        img_url = urljoin(base_url, img_url)
    valid_extensions = ['.jpg', '.jpeg', '.png', '.webp', '.gif']
    if any(img_url.lower().endswith(ext) for ext in valid_extensions):
        return img_url
    base_name = img_url.split('/')[-1].split('?')[0]
    for ext in ['.jpg', '.jpeg', '.png', '.webp']:
        pattern = rf'{re.escape(base_name)}{ext}'
        if re.search(pattern, html_content, re.IGNORECASE):
            complete_url = img_url + ext
            logger.info(f"✓ Completed URL: {img_url} → {complete_url}")
            return complete_url
    logger.warning(f"⚠ Guessing extension for: {img_url}")
    return img_url + '.jpg'
def is_valid_product_image(url: str, mpn: str) -> bool:
    
    url_lower = url.lower()
    exclude_terms = [
        'logo', 'icon', 'badge', 'banner', 'social',
        'facebook', 'twitter', 'instagram',
        'thumbnail', 'thumb', '50x50', '100x100',
        'avatar', 'profile', 'author'
    ]
    if any(term in url_lower for term in exclude_terms):
        return False
    if not any(url_lower.endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp', '.gif']):
        return False
    return True
def score_image_url(url: str, mpn: str) -> int:
    
    score = 0
    url_lower = url.lower()
    mpn_lower = mpn.lower()
    if mpn_lower in url_lower:
        score += 100
    if any(x in url_lower for x in ['1200', '1500', '2000', 'large', 'zoom', 'hires', 'original']):
        score += 50
    if any(x in url_lower for x in ['product', 'main', 'hero', 'primary']):
        score += 30
    if any(x in url_lower for x in ['thumb', 'small', '100', '150', '200']):
        score -= 50
    if any(x in url_lower for x in ['logo', 'icon', 'badge', 'social']):
        score -= 100
    return score
def extract_best_image_fallback(all_extractions: List[Dict]) -> Optional[str]:
    for source in all_extractions:
        img_url = source.get('image_url')
        if img_url and isinstance(img_url, str) and img_url.strip():
            if img_url.startswith('http'):
                logger.info(f"Using image from {source['url']}: {img_url[:80]}")
                return img_url.strip()
    logger.warning("No valid image URL found in any source")
    return None