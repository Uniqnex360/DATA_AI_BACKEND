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
    