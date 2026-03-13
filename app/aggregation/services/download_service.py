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
            async with httpx.AsyncClient(
                verify=False,
                headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        }
            ) as client:

                response = await client.get(
                    url,
                    timeout=self.timeout,
                    follow_redirects=True
                )

                if response.status_code in [403, 401, 429]:
                    return None

                if response.status_code != 200:
                    return None

                is_pdf = "pdf" in response.headers.get("Content-Type", "").lower()

                return {
                    "source_url": url,
                    "raw_bytes": response.content,
                    "type": "pdf" if is_pdf else "html",
                }

        except Exception:
            return None