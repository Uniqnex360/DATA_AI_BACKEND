import logging
import httpx
from typing import Dict, Optional
from app.aggregation.interfaces import IDownloadService
logger = logging.getLogger("download_service")
import random
class HttpDownloadService(IDownloadService):

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15'
        ]

    async def download(self, url: str) -> Optional[Dict]:
        try:
            async with httpx.AsyncClient(
                verify=False,
                headers = {
    'User-Agent': random.choice(self.user_agents),  
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'DNT': '1',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Cache-Control': 'max-age=0',
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