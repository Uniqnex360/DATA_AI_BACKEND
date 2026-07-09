
import asyncio
import logging
import hashlib
import time
from typing import Dict, List, Optional,Any

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
MAX_SERP_CALLS = 3


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
        title: str = None,
        brand: Optional[str] = None, 
        taxonomy: Optional[str] = None, 
        prompt_config: Optional[Dict[str, Any]] = None
    ) -> Dict:
        request_id = hashlib.sha256(f"{mpn}{title}{time.time()}".encode()).hexdigest()[:12]

        logger.info(f"[{request_id}] Pipeline started for {mpn or title}")
        if prompt_config:
            logger.info(f"[{request_id}] Mode: {prompt_config.get('mode', 'unknown')}")
            logger.info(f"[{request_id}] Expected attributes: {prompt_config.get('expected_attributes', [])[:5]}")

        identifiers = {
            "mpn": mpn or "",
            "upc": upc or "",
            "title": title or "",
            "brand": brand or (title or "").split(maxsplit=1)[0] if title else ""

        }
    
        queries = generate_search_queries(mpn, identifiers["brand"], title)
        if not queries:
            queries = [f"{mpn} datasheet pdf", f"{title} specifications"]
        logger.info(f"[{request_id}] Search queries: {queries}")
        urls: List[str] = []
        for q in queries:
            found = await self.search.get_urls(q,mpn,brand,title)
            urls.extend(found)
            if len(urls)>MAX_SOURCES:
                break
            await asyncio.sleep(0.1)
        logger.info(f"[{request_id}] Found {len(urls)} URLs")
        download_tasks = [self.downloader.download(url) for url in urls]
        results = await asyncio.gather(*download_tasks, return_exceptions=True)

        sources = []
        for i, result in enumerate(results):
            if len(sources) >= MAX_SOURCES:
                break
            if isinstance(result, Exception) or not result:
                continue
            sources.append(result)
        logger.info(f"[{request_id}] Downloaded {len(sources)} sources")
        final_image_url = await self.image_service.extract_best_image(
            sources=sources, request_id=request_id,mpn=mpn or "",brand=brand or "",source_urls=urls
        )
        
        
        if not final_image_url and (mpn or title):
            logger.info(f"[{request_id}] No image found in initial sources, attempting image-specific searches...")
            image_queries = []
            if mpn:
                image_queries.extend([
                    f"{mpn} product image",
                    f"{mpn} photo",
                    f"{mpn} official image"
                ])
            if title:
                image_queries.extend([
                    f"{title} product image",
                    f"{title} photo official"
                ])
            
            
            image_urls = []
            for q in image_queries[:3]:  
                try:
                    found = await self.search.get_urls(q,mpn=mpn or "",brand=brand or identifiers["brand"] or "",title=title or "")
                    image_urls.extend(found)
                    if len(image_urls) >= 3:
                        break
                    await asyncio.sleep(0.1)
                except Exception as e:
                    logger.info(f"[{request_id}] Image search query failed: {e}")
            
            
            if image_urls:
                logger.info(f"[{request_id}] Downloading {len(image_urls)} additional URLs for image extraction")
                image_download_tasks = [self.downloader.download(url) for url in image_urls]
                image_results = await asyncio.gather(*image_download_tasks, return_exceptions=True)
                
                image_sources = []
                for result in image_results:
                    if isinstance(result, Exception) or not result:
                        continue
                    image_sources.append(result)
                
                if image_sources:
                    final_image_url = await self.image_service.extract_best_image(
    sources=image_sources,
    request_id=request_id,
    mpn=mpn or "",
    brand=brand or "",
    source_urls=image_urls   
)
                    if final_image_url:
                        logger.info(f"[{request_id}] ✓ SUCCESS: Found image through retry mechanism")
                    else:
                        logger.warning(f"[{request_id}] Could not extract image from retry sources")
        
        if not final_image_url:
            logger.warning(f"[{request_id}] WARNING: No valid image found after exhausting all strategies")

        extract_tasks = [
            self.extractor.extract(
                src,
                prompt_config=prompt_config 
            ) 
            for src in sources
        ]
        extracted_results = await asyncio.gather(*extract_tasks, return_exceptions=True)

        extracted = [
            r for r in extracted_results
            if r and not isinstance(r, Exception)
        ]
        logger.info(f"[{request_id}] Extracted data from {len(extracted)}/{len(sources)} sources")
        if not extracted:
            if final_image_url:
                return {
                    "status": "success",
                    "image_url": final_image_url,
                    "golden_record": {"attributes": {}, "ready_for_publish": False,"sources_consulted": urls },
                }
            return {"status": "failed", "reason": "No specifications found"}

        keys = [k for e in extracted for k in e.get("attributes", {}).keys()]
        unique_keys = list(set(keys))
        mapping = await asyncio.to_thread(unify_attributes, unique_keys)
        logger.info(f"[{request_id}] Found {len(unique_keys)} unique attribute names")
        standardized = {}
        canonical_map = mapping.get("canonical_attributes", {})
        if not canonical_map and "schema" in mapping:
            canonical_map = mapping["schema"].get("canonical_attributes", {})
        if not canonical_map:
            logger.warning(f"[{request_id}] Mapping empty, treating unique keys as canonical")
            canonical_map = {k: {"synonyms": [k]} for k in unique_keys}
        for canonical, info in canonical_map.items():
            values = []
            synonyms = [s.lower().strip() for s in info.get("synonyms", [])]
            for e in extracted:
                raw_attrs = e.get("attributes", {})
                norm_attrs = {str(k).lower().strip(): v for k, v in raw_attrs.items()}
                for syn in synonyms:
                    if syn in norm_attrs:
                        val = norm_attrs[syn]
                        if val not in [None, "", "N/A", "null", "None"]:
                            values.append(val)
            if values:
                unique_vals = list(set(str(v) for v in values))
                std_result = await asyncio.to_thread(
                    standardize_with_llm, canonical, unique_vals
                )
                if std_result and std_result.get("standard_value") is not None:
                    standardized[canonical] = std_result
                else:
                    standardized[canonical] = {"standard_value": values[0], "derived_from": values}
        if not standardized:
            logger.error(f"[{request_id}] No valid data survived standardization")
                         
        golden = await asyncio.to_thread(
            build_golden_record, 
            standardized, 
            identifiers,
            scraped_urls=urls,
            taxonomy=taxonomy,
            primary_attributes=prompt_config.get('expected_attributes') if prompt_config else None
        )
        if prompt_config and prompt_config.get('mode') == 'constrained':
            expected = prompt_config.get('expected_attributes', [])
            found_attrs = set(golden.get('attributes', {}).keys())
            missing_primary = [
                attr for attr in expected 
                if attr != "*additional*" and attr not in found_attrs
            ]
            
            if missing_primary:
                logger.warning(
                    f"[{request_id}] Missing primary attributes: {missing_primary}"
                )
                golden['missing_primary_attributes'] = missing_primary

        return {
            "request_id": request_id,
            "identifiers": identifiers,
            "sources_used": len(sources),
            "image_url": final_image_url,
            "golden_record": golden,
            "status": "success",
        }