import logging
import hashlib
import time
import shutil
import tempfile
from typing import Dict, List, Optional
from pathlib import Path
import requests
from app.extractors import extract_pdf_pdfplumber, extract_web_playwright
from .cloudinary_client import upload_source
from app.core.config import settings
from concurrent.futures import ThreadPoolExecutor, as_completed
logger = logging.getLogger("truth_engine")
logger.setLevel(logging.INFO)
MAX_SOURCES = 3
MAX_SERP_CALLS = 1


from app.sacred  import (
    generate_search_queries,
    extract_from_web,
    extract_from_pdf,
    standardize_with_llm,
    build_golden_record,
    unify_attributes
)
def download_and_store(url: str) -> Optional[Dict]:
   
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "TruthEngine/1.0"},
            timeout=30,
            verify=False
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


def get_serp_urls(query: str) -> List[str]:
    if not settings.serpapi_key:
        logger.error("SerpAPI key is missing!")
        return []
    try:
        response = requests.get(
            "https://serpapi.com/search",
            params={
                "engine": "google",
                "q": query,
                "api_key": settings.serpapi_key,
                "num": 10,
            },
            timeout=20,
        )
        data = response.json()
        urls = []
        for r in data.get("organic_results", []):
            link = r.get("link")
            if link:
                urls.append(link)
        
        return urls[:5] 
    except Exception as e:
        logger.warning(f"SerpAPI failed for '{query}': {e}")
        return []

async def aggregate_product(mpn: str = None, upc: str = None, title: str = None) -> Dict:
    request_id = hashlib.sha256(f"{mpn}{title}{time.time()}".encode()).hexdigest()[:12]
    logger.info(f"[{request_id}] Aggregation started for {mpn or title}")

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
        urls.extend(get_serp_urls(q))
        time.sleep(0.1)

    sources = []
    seen = set()

    with ThreadPoolExecutor(max_workers=5) as pool:
        future_to_url = {pool.submit(download_and_store, url): url for url in urls}
        
        for future in as_completed(future_to_url):
            if len(sources) >= MAX_SOURCES: break
            url = future_to_url[future]
            
            try:
                src = future.result()
                
                if not src:
                    logger.info(f"Standard request failed for {url}, trying Playwright...")
                    html_content = await extract_web_playwright(url)
                    if html_content:
                        src = {
                            "source_url": url,
                            "raw_bytes": html_content.encode('utf-8'),
                            "type": "html"
                        }

                if src:
                    sources.append(src)
                    seen.add(url)
            except Exception as e:
                logger.error(f"Failed to process URL {url}: {e}")

    extracted = []
    for src in sources:
        try:
            if src["type"] == "pdf":
                with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
                    tmp.write(src["raw_bytes"])
                    raw_text = extract_pdf_pdfplumber(tmp.name)
                    data = extract_from_pdf(raw_text)
            else:
                html_text = src["raw_bytes"].decode('utf-8', errors='ignore')
                data = extract_from_web(html_text)
            
            data["source_url"] = src["source_url"]
            data["raw_content"] = src["raw_bytes"] 
            extracted.append(data)
        except Exception as e:
            logger.warning("Extraction failed for %s: %s", src.get('source_url'), str(e))

    if not extracted:
        return {"status": "failed", "reason": "No specifications found across sources"}

    keys = [k for e in extracted for k in e.get("attributes", {}).keys()]
    mapping = unify_attributes(list(set(keys)))
    
    standardized = {}
    canonical_map = mapping.get("canonical_attributes", {})
    for canonical, info in canonical_map.items():
        values = []
        for e in extracted:
            for syn in info.get("synonyms", []):
                if syn in e.get("attributes", {}):
                    values.append(e["attributes"][syn])
        if values:
            standardized[canonical] = standardize_with_llm(canonical, values)

    golden = build_golden_record(standardized, identifiers)
    
    return {
        "request_id": request_id,
        "identifiers": identifiers,
        "sources_used": len(sources),
        "sources_data": sources, 
        "golden_record": golden,
        "ready_for_publish": golden.get("ready_for_publish", False),
        "status": "success",
    }