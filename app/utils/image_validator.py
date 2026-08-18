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
     'model-plate', 'model_plate', 'find_model', 'badge',
    'guide', 'watermark', 'thumb_', '_th.'
]
import re, json

_MOZU_PRELOAD_RE = re.compile(
    r'id="data-mz-preload-product">\s*(\{.*?\})\s*</script>',
    re.DOTALL
)
import json
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
def is_manufacturer_domain(src_host: str, mfg_hosts: set, brand: Optional[str] = None) -> bool:
    src_host = src_host.lower().replace("www.", "")
    
    for mh in mfg_hosts:
        mh = mh.lower().replace("www.", "")
        # 1. Direct or substring match
        if mh in src_host or src_host in mh:
            return True
            
        # 2. Root domain match for regional variants (e.g., whirlpool.com vs whirlpool.co.uk)
        mh_parts = mh.split('.')
        src_parts = src_host.split('.')
        if len(mh_parts) >= 2 and len(src_parts) >= 2:
            # If the root domain matches (e.g., 'whirlpool' == 'whirlpool')
            if mh_parts[-2] == src_parts[-2]:
                return True

    # 3. Fallback: check if the brand slug appears in the host
    if brand:
        brand_slug = brand.lower().replace(" ", "")
        if brand_slug in src_host:
            return True
            
    return False
def extract_universal_product_images(html_text: str, base_url: str) -> list[str]:
   
    if not html_text:
        return []

    found_images = []
    seen = set()

    def _add_image(img_url: str):
        if not img_url or not isinstance(img_url, str):
            return
        img_url = img_url.strip()
        if img_url.startswith("//"):
            img_url = "https:" + img_url
        elif img_url.startswith("/"):
            img_url = urljoin(base_url, img_url)
        if not img_url.startswith(("http://", "https://")):
            return
        from urllib.parse import urlparse
        path = urlparse(img_url).path.lower()
        if not any(path.endswith(ext) for ext in ('.jpg', '.jpeg', '.png', '.webp', '.gif')):
            return
        
        lower_url = img_url.lower()
        if any(b in lower_url for b in BLOCKED_KEYWORDS):
            return


        if img_url not in seen:
            seen.add(img_url)
            found_images.append(img_url)

    
    try:
        soup = BeautifulSoup(html_text, 'html.parser')
        for script in soup.find_all('script', type='application/ld+json'):
            if not script.string:
                continue
            try:
                data = json.loads(script.string)
                
                items = data if isinstance(data, list) else [data]
                for item in items:
                    
                    if isinstance(item, dict) and item.get('@type') in ['Product', 'IndividualProduct']:
                        imgs = item.get('image')
                        if isinstance(imgs, list):
                            for img in imgs:
                                _add_image(img if isinstance(img, str) else img.get('url'))
                        elif isinstance(imgs, str):
                            _add_image(imgs)
                        elif isinstance(imgs, dict):
                            _add_image(imgs.get('url'))
            except Exception:
                continue
    except Exception:
        pass

    
    try:
        if 'soup' not in locals():
            soup = BeautifulSoup(html_text, 'html.parser')

        
        for meta in soup.find_all('meta', property=re.compile(r'og:image|twitter:image', re.I)):
            _add_image(meta.get('content'))

        
        for img in soup.find_all(['img', 'source', 'a']):
            
            for attr in ['data-zoom-image', 'data-large-img', 'data-src', 'data-high-res', 'data-zoom', 'srcset', 'src', 'href']:
                val = img.get(attr)
                if val:
                    
                    if attr == 'srcset':
                        for part in val.split(','):
                            src_url = part.strip().split(' ')[0]
                            if any(src_url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp']):
                                _add_image(src_url)
                    elif any(val.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp']):
                        _add_image(val)
    except Exception:
        pass

    return found_images[:8]
def extract_mozu_preload_images(html_text: str) -> list[str]:
    m = _MOZU_PRELOAD_RE.search(html_text or "")
    if not m:
        return []

    try:
        data = json.loads(m.group(1))
    except Exception:
        return []

    images = []

    
    main = data.get("mainImage") or {}
    for k in ("src", "imageUrl"):
        if main.get(k):
            images.append(main[k])

    
    content = data.get("content") or {}
    for img in content.get("productImages") or []:
        src = img.get("src") or img.get("imageUrl")
        if src:
            images.append(src)

    
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