
from abc import ABC, abstractmethod
from typing import Dict, List, Optional
import httpx


class ISearchService(ABC):
    
    @abstractmethod
    async def get_urls(self, query: str) -> List[str]:
        pass


class IDownloadService(ABC):
    
    @abstractmethod
    async def download(self, url: str) -> Optional[Dict]:
        pass


class IExtractor(ABC):
    
    @abstractmethod
    def can_handle(self, content_type: str) -> bool:
        pass
    
    @abstractmethod
    async def extract(self, raw_bytes: bytes, url: str) -> Dict:
        pass


class IImageService(ABC):
    
    @abstractmethod
    async def extract_best_image(self,sources: list,request_id: str,mpn: str = "",brand: str = "",source_urls: list = None,) -> Optional[str]:
        pass


class IGoldenRecordBuilder(ABC):
    
    @abstractmethod
    async def build(self, extracted: List[Dict], identifiers: Dict) -> Dict:
        pass