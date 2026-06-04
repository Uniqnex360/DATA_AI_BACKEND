import httpx
import logging
from typing import Optional
logger = logging.getLogger("image_validator")
BLOCKED_KEYWORDS = [
    'logo', 'icon', 'carton', 'banner', 'default',
    'no-image', 'noimage', 'placeholder', 'missing',
    'blank', 'spacer', 'pixel', 'tracking',
    'social-share', 'social_share',
    'facebook', 'twitter', 'og-image',
    'share.jpg', 'share.png',
    'camozzi', '3d-carton',
]


async def validate_image_url(url: str, max_retries: int = 2) -> bool:
    if not url:
        return False
    if await _test_image_url(url):
        return True
    if not any(url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp']):
        for ext in ['.jpg', '.jpeg', '.png', '.webp']:
            test_url = url + ext
            if await _test_image_url(test_url):
                logger.info(f"✓ Found working URL with extension: {test_url}")
                return True
    return False


async def _test_image_url(url: str) -> bool:
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
        'Referer': 'https://www.google.com/'  
    }
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True, verify=False) as client:
            # 2. Try HEAD first (fast)
            response = await client.head(url, headers=headers)
            
            if response.status_code != 200:
                headers['Range'] = 'bytes=0-1024'
                response = await client.get(url, headers=headers)

            if response.status_code in [200, 206]:
                content_type = response.headers.get('content-type', '').lower()
                if 'image' in content_type:
                    return True
                
                if response.content.startswith((b'\xff\xd8', b'\x89PNG', b'RIFF')):
                    return True
    except Exception as e:
        logger.debug(f"Image validation failed: {e}")
    return False