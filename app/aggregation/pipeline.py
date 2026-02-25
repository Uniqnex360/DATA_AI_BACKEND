
import asyncio
import logging
import hashlib
import time
from typing import Dict, List, Optional

from app.sacred import (
    generate_search_queries,
    unify_attributes,
    standardize_with_llm,
    build_golden_record
)
from app.aggregation.interfaces import ISearchService, IDownloadService, IImageService
from app.aggregation.services.extraction_service import ExtractionService

logger = logging.getLogger("pipeline")

MAX_SOURCES = 3
MAX_SERP_CALLS = 1


class AggregationPipeline:
    
    def __init__(
        self,
        search_service: ISearchService,
        download_service: IDownloadService,
        extraction_service: ExtractionService,
        image_service: IImageService,
    ):
        self.search = search_service
        self.downloader = download_service
        self.extractor = extraction_service
        self.image_service = image_service
    
    async def run(
        self,
        mpn: str = None,
        upc: str = None,
        title: str = None
    ) -> Dict:
        request_id = hashlib.sha256(
            f"{mpn}{title}{time.time()}".encode()
        ).hexdigest()[:12]

        logger.info(f"[{request_id}] Pipeline started for {mpn or title}")

        identifiers = {
            "mpn": mpn or "",
            "upc": upc or "",
            "title": title or "",
            "brand": (title or "").split(maxsplit=1)[0] if title else "",
        }

        queries = generate_search_queries(mpn, identifiers["brand"], title)
        if not queries:
            queries = [f"{mpn} datasheet pdf", f"{title} specifications"]

        urls: List[str] = []
        for q in queries[:MAX_SERP_CALLS]:
            found = await self.search.get_urls(q)
            urls.extend(found)
            await asyncio.sleep(0.1)

        download_tasks = [self.downloader.download(url) for url in urls]
        results = await asyncio.gather(*download_tasks, return_exceptions=True)

        sources = []
        for i, result in enumerate(results):
            if len(sources) >= MAX_SOURCES:
                break
            if isinstance(result, Exception) or not result:
                continue
            sources.append(result)

        final_image_url = await self.image_service.extract_best_image(
            sources, request_id
        )

        extract_tasks = [self.extractor.extract(src) for src in sources]
        extracted_results = await asyncio.gather(*extract_tasks, return_exceptions=True)

        extracted = [
            r for r in extracted_results
            if r and not isinstance(r, Exception)
        ]

        if not extracted:
            if final_image_url:
                return {
                    "status": "success",
                    "image_url": final_image_url,
                    "golden_record": {"attributes": {}, "ready_for_publish": False},
                }
            return {"status": "failed", "reason": "No specifications found"}

        keys = [k for e in extracted for k in e.get("attributes", {}).keys()]
        mapping = await asyncio.to_thread(unify_attributes, list(set(keys)))

        standardized = {}
        canonical_map = mapping.get("canonical_attributes", {})
        for canonical, info in canonical_map.items():
            values = []
            for e in extracted:
                for syn in info.get("synonyms", []):
                    if syn in e.get("attributes", {}):
                        values.append(e["attributes"][syn])
            if values:
                standardized[canonical] = await asyncio.to_thread(
                    standardize_with_llm, canonical, values
                )

        golden = await asyncio.to_thread(
            build_golden_record, standardized, identifiers
        )

        return {
            "request_id": request_id,
            "identifiers": identifiers,
            "sources_used": len(sources),
            "image_url": final_image_url,
            "golden_record": golden,
            "status": "success",
        }