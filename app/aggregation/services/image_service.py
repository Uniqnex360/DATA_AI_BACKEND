import logging
from typing import Optional
from app.sacred import extract_image_from_source
from app.aggregation.interfaces import IImageService
from app.utils.image_validator import validate_image_url
logger = logging.getLogger("image_service")
OFFICIAL_DOMAINS = [
    'apple.com', 'dell.com', 'lenovo.com',
    'samsung.com', 'hp.com', 'logitech.com'
]


class ImageService(IImageService):
    def __init__(self, official_domains: list = None):
        self.official_domains = official_domains or OFFICIAL_DOMAINS

    def is_official(self, url: str) -> bool:
        return any(domain in url for domain in self.official_domains)

    async def extract_best_image(
        self,
        sources: list,
        request_id: str
    ) -> Optional[str]:
        official_images = []
        candidate_images = []
        for src in sources:
            url = src.get("source_url", "")
            html_text = src["raw_bytes"].decode('utf-8', errors='ignore')
            try:
                img = await extract_image_from_source(html_text, url)
                if img:
                    if self.is_official(url):
                        official_images.append(img)
                        logger.info(
                            f"[{request_id}] Found official image: {img}")
                    else:
                        candidate_images.append(img)
                        logger.info(
                            f"[{request_id}] Found candidate image: {img}")
            except Exception as e:
                logger.warning(
                    f"[{request_id}] Image extraction failed for {url}: {e}")
        if official_images:
            for img_url in official_images:
                if await validate_image_url(img_url):
                    logger.info(
                        f"[{request_id}] ✓ Using validated official image: {img_url}")
                    return img_url
            if official_images:
                logger.warning(
                    f"[{request_id}] Official image validation failed, using unvalidated: {official_images[0]}")
                return official_images[0]
        if candidate_images:
            for img_url in candidate_images:
                if await validate_image_url(img_url):
                    logger.info(
                        f"[{request_id}] ✓ Using validated candidate image: {img_url}")
                    return img_url
            if candidate_images:
                logger.warning(
                    f"[{request_id}] Candidate image validation failed, using unvalidated: {candidate_images[0]}")
                return candidate_images[0]
        logger.warning(f"[{request_id}] No images found in any sources")
        return None
