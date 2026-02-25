
import logging
from typing import Optional
from app.sacred import extract_image_from_source
from app.aggregation.interfaces import IImageService

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
        final_image = None
        
        for src in sources:
            url = src.get("source_url", "")
            html_text = src["raw_bytes"].decode('utf-8', errors='ignore')
            
            try:
                img = await extract_image_from_source(html_text, url)
                if img:
                    if self.is_official(url):
                        logger.info(f"[{request_id}] Official image: {img}")
                        return img
                    elif not final_image:
                        final_image = img
            except Exception as e:
                logger.warning(f"Image extraction failed: {e}")
        
        return final_image