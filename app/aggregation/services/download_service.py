
import logging
import httpx
from typing import Dict, Optional
from app.aggregation.interfaces import IDownloadService

logger = logging.getLogger("download_service")


class HttpDownloadService(IDownloadService):
    
    def __init__(self, timeout: int = 30):
        self.timeout = timeout
    
    async def download(self, url: str) -> Optional[Dict]:
        try:
            async with httpx.AsyncClient(verify=False) as client:
                response = await client.get(
                    url,
                    headers={"User-Agent": "TruthEngine/1.0"},
                    timeout=self.timeout,
                    follow_redirects=True
                )
                if response.status_code != 200:
                    return None
                
                is_pdf = "pdf" in response.headers.get("Content-Type", "").lower()
                
                return {
                    "source_url": url,
                    "raw_bytes": response.content,
                    "type": "pdf" if is_pdf else "html",
                }
        
        except Exception as e:
            logger.warning(f"Download failed {url}: {e}")
            return None