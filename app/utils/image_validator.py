import httpx
import logging
from typing import Optional

logger = logging.getLogger("image_validator")

BLOCKED_KEYWORDS = [
        'logo', 'icon', 'carton', 'banner', 'default',
        'no-image', 'noimage', 'placeholder', 'missing',
        'blank', 'spacer', 'pixel', 'tracking',
        'social-share', 'social_share',       # ← catches pension-evaluators
        'facebook', 'twitter', 'og-image',    # ← catches yumpu facebook image
        'share.jpg', 'share.png',
        'camozzi', '3d-carton',               # ← catches kyklo logo
    ]


async def validate_image_url(url: str, timeout: int = 10) -> bool:
    if not url or not isinstance(url, str) or not url.strip():
        logger.debug(f"Invalid image URL format: {url}")
        return False
    
    url = url.strip()
    if any(keyword.lower() in url.lower() for keyword in BLOCKED_KEYWORDS):
        logger.debug(f"Image URL contains blocked keyword: {url}")
        return False
    
    try:
        async with httpx.AsyncClient(
            verify=False,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0"}
        ) as client:
            response = await client.head(url, follow_redirects=True)
            
            if response.status_code not in [200, 301, 302, 304]:
                logger.debug(f"Invalid status code {response.status_code} for {url}")
                return False
            
            content_type = response.headers.get("content-type", "").lower()
            if not any(itype in content_type for itype in ['image/jpeg', 'image/png', 'image/webp', 'image/gif']):
                logger.debug(f"Invalid content-type {content_type} for {url}")
                return False
            
            content_length = response.headers.get("content-length")
            if content_length:
                size_bytes = int(content_length)
                if size_bytes < 1000:
                    logger.debug(f"Image too small ({size_bytes} bytes): {url}")
                    return False
                if size_bytes > 50 * 1024 * 1024:
                    logger.debug(f"Image too large ({size_bytes} bytes): {url}")
                    return False
            
            logger.info(f"✓ Valid image URL: {url}")
            return True
            
    except httpx.TimeoutException:
        logger.warning(f"Timeout validating image: {url}")
        return False
    except Exception as e:
        logger.warning(f"Error validating image URL {url}: {e}")
        return False
