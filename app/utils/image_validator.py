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
import re, json

_MOZU_PRELOAD_RE = re.compile(
    r'id="data-mz-preload-product">\s*(\{.*?\})\s*</script>',
    re.DOTALL
)

def extract_mozu_preload_images(html_text: str) -> list[str]:
    m = _MOZU_PRELOAD_RE.search(html_text or "")
    if not m:
        return []

    try:
        data = json.loads(m.group(1))
    except Exception:
        return []

    images = []

    # main image
    main = data.get("mainImage") or {}
    for k in ("src", "imageUrl"):
        if main.get(k):
            images.append(main[k])

    # carousel images
    content = data.get("content") or {}
    for img in content.get("productImages") or []:
        src = img.get("src") or img.get("imageUrl")
        if src:
            images.append(src)

    # normalize + dedupe (preserve order)
    out, seen = [], set()
    for u in images:
        if u.startswith("//"):
            u = "https:" + u
        if u and u not in seen:
            out.append(u)
            seen.add(u)
    return out

async def validate_image_url(url: str, max_retries: int = 2) -> bool:
    url = (url or "").strip()
    if not url:
        return False

    if url.startswith("//"):
        url = "https:" + url
    if await _test_image_url(url):
        return True
    if not any(url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp']):
        for ext in ['.jpg', '.jpeg', '.png', '.webp', '.gif']:
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
        logger.info(f"Image validation failed: {e}")
    return False