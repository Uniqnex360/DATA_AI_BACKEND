# import logging
# import hashlib
# import time
# import tempfile
# from typing import Dict, List, Optional
# from app.extractors import extract_pdf_pdfplumber, extract_web_playwright
# from app.core.config import settings
# from app.sacred import extract_image_from_source
# logger = logging.getLogger("truth_engine")
# import httpx
# import asyncio

# logger.setLevel(logging.INFO)
# MAX_SOURCES = 3
# MAX_SERP_CALLS = 1


# from app.sacred  import (
#     generate_search_queries,
#     extract_from_web,
#     extract_from_pdf,
#     standardize_with_llm,
#     build_golden_record,
#     unify_attributes
# )

# async def download_and_store(client:httpx.AsyncClient,url: str) -> Optional[Dict]:
   
#     try:
#         response = await client.get(
#             url,
#             headers={"User-Agent": "TruthEngine/1.0"},
#             timeout=30,
#             follow_redirects=True
#         )
#         if response.status_code != 200:
#             return None

#         is_pdf = "pdf" in response.headers.get("Content-Type", "").lower()
        
#         return {
#             "source_url": url,
#             "raw_bytes": response.content, 
#             "type": "pdf" if is_pdf else "html",
#         }
#     except Exception as e:
#         logger.warning(f"Download failed {url}: {e}")
#         return None


# async def get_serp_urls(client: httpx.AsyncClient, query: str) -> List[str]:
#     if not settings.serpapi_key:
#         logger.error("SerpAPI key is missing!")
#         return []
#     try:
#         response = await client.get(
#             "https://serpapi.com/search",
#             params={
#                 "engine": "google",
#                 "q": query,
#                 "api_key": settings.serpapi_key,
#                 "num": 10,
#             },
#             timeout=20,
#         )
#         data = response.json()
#         urls = []
#         for r in data.get("organic_results", []):
#             link = r.get("link")
#             if link:
#                 urls.append(link)

#         return urls[:5]
#     except Exception as e:
#         logger.warning(f"SerpAPI failed for '{query}': {e}")
#         return []
    
# async def aggregate_product(mpn: str = None, upc: str = None, title: str = None) -> Dict:
#     request_id = hashlib.sha256(f"{mpn}{title}{time.time()}".encode()).hexdigest()[:12]
#     logger.info(f"[{request_id}] Aggregation started for {mpn or title}")
#     final_image_url = None
#     official_domains = ['apple.com', 'dell.com', 'lenovo.com', 'samsung.com', 'hp.com']
#     identifiers = {
#         "mpn": mpn or "",
#         "upc": upc or "",
#         "title": title or "",
#         "brand": (title or "").split(maxsplit=1)[0] if title else "",
#     }

#     queries = generate_search_queries(mpn, identifiers["brand"], title)
#     if not queries:
#         queries = [f"{mpn} datasheet pdf", f"{title} specifications"]

#     async with httpx.AsyncClient(verify=False) as client:

        
#         urls: List[str] = []
#         for q in queries[:MAX_SERP_CALLS]:
#             serp_urls = await get_serp_urls(client, q)
#             urls.extend(serp_urls)
#             await asyncio.sleep(0.1) 

#         download_tasks = [download_and_store(client, url) for url in urls]
#         download_results = await asyncio.gather(*download_tasks, return_exceptions=True)

#         sources = []
#         for i, result in enumerate(download_results):
#             if len(sources) >= MAX_SOURCES:
#                 break

#             if isinstance(result, Exception):
#                 logger.warning(f"Download error for {urls[i]}: {result}")
#                 continue

#             src = result
#             url = urls[i]

#             if not src:
#                 logger.info(f"Standard request failed for {url}, trying Playwright...")
#                 try:
#                     html_content = await extract_web_playwright(url)
#                     if html_content:
#                         src = {
#                             "source_url": url,
#                             "raw_bytes": html_content.encode('utf-8'),
#                             "type": "html"
#                         }
#                 except Exception as e:
#                     logger.error(f"Playwright failed for {url}: {e}")

#             if src:
#                 sources.append(src)

#     extracted = []
#     for src in sources:
#         current_url = src.get("source_url")
#         html_text = src["raw_bytes"].decode('utf-8', errors='ignore')
#         is_official = any(domain in current_url for domain in official_domains)

#         img = await extract_image_from_source(html_text, current_url)
#         if img:
#             if is_official:
#                 logger.info(f"[{request_id}] ✓ FOUND OFFICIAL IMAGE: {img}")
#                 final_image_url = img
#             elif not final_image_url:
#                 final_image_url = img

#         try:
#             if src["type"] == "pdf":
#                 def _extract_pdf(raw_bytes):
#                     with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
#                         tmp.write(raw_bytes)
#                         tmp_path = tmp.name
#                     try:
#                         raw_text = extract_pdf_pdfplumber(tmp_path)
#                         return extract_from_pdf(raw_text)
#                     finally:
#                         import os
#                         os.unlink(tmp_path)

#                 data = await asyncio.to_thread(_extract_pdf, src["raw_bytes"])
#             else:
                
#                 data = await asyncio.to_thread(extract_from_web, html_text)

#             data["source_url"] = src["source_url"]
#             extracted.append(data)
#         except Exception as e:
#             logger.warning("Extraction failed for %s. Error type: %s", current_url, type(e).__name__)

#     if not extracted:
#         if final_image_url:
#             return {
#                 "status": "success",
#                 "image_url": final_image_url,
#                 "golden_record": {"attributes": {}, "ready_for_publish": False},
#                 "reason": "Image found but no text specs discovered"
#             }
#         return {"status": "failed", "reason": "No specifications found across sources"}

    
#     keys = [k for e in extracted for k in e.get("attributes", {}).keys()]
#     mapping = await asyncio.to_thread(unify_attributes, list(set(keys)))

#     standardized = {}
#     canonical_map = mapping.get("canonical_attributes", {})
#     for canonical, info in canonical_map.items():
#         values = []
#         for e in extracted:
#             for syn in info.get("synonyms", []):
#                 if syn in e.get("attributes", {}):
#                     values.append(e["attributes"][syn])
#         if values:
#             standardized[canonical] = await asyncio.to_thread(
#                 standardize_with_llm, canonical, values
#             )

#     golden = await asyncio.to_thread(build_golden_record, standardized, identifiers)

#     return {
#         "request_id": request_id,
#         "identifiers": identifiers,
#         "sources_used": len(sources),
#         "sources_data": [
#         {"source_url": s["source_url"], "type": s["type"]} 
#         for s in sources], 
#         "image_url": final_image_url,
#         "golden_record": golden,
#         "ready_for_publish": golden.get("ready_for_publish", False),
#         "status": "success",
#     }