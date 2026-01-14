import logging
import hashlib
import time
import shutil
import tempfile
from typing import Dict, List, Optional
from pathlib import Path
import requests
from app.main import unify_attributes
from app.extractors import extract_pdf_pdfplumber

from .sacred import (
    generate_search_queries,
    extract_from_web,
    extract_from_pdf,
    standardize_with_llm,
    build_golden_record,
)
from .cloudinary_client import upload_source
from .config import settings
logger = logging.getLogger("truth_engine")
logger.setLevel(logging.INFO)
MAX_SOURCES = 3
MAX_SERP_CALLS = 1


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
def download_and_store(url: str, temp_dir: Path) -> Optional[Dict]:
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "TruthEngine/1.0"},
            timeout=25,
        )
        if response.status_code != 200:
            return None
        content_hash = hashlib.sha256(response.content).hexdigest()[:16]
        is_pdf = "pdf" in response.headers.get("Content-Type", "").lower()
        ext = ".pdf" if is_pdf else ".html"
        local_path = temp_dir / f"{content_hash}{ext}"
        local_path.write_bytes(response.content)
        upload_result = upload_source(response.content, content_hash)
        if not upload_result:
            return None
        return {
            "source_url": url,
            "cloudinary_url": upload_result.get("secure_url"),
            "local_path": str(local_path),
            "type": "pdf" if is_pdf else "html",
        }
    except Exception as e:
        logger.warning(f"Download failed {url}: {e}")
        return None


def aggregate_product(mpn: str = None, upc: str = None, title: str = None) -> Dict:
    request_id = hashlib.sha256(
        f"{mpn}{title}{time.time()}".encode()).hexdigest()[:12]
    logger.info(f"[{request_id}] Aggregation started")
    identifiers = {
        "mpn": mpn or "",
        "upc": upc or "",
        "title": title or "",
        "brand": (title or "").split(maxsplit=1)[0] if title else "",
    }
    with tempfile.TemporaryDirectory(prefix="truth_") as tmp:
        temp_dir = Path(tmp)
        queries = generate_search_queries(mpn, identifiers["brand"], title)
        if not queries:
            queries = [
                f"{mpn} datasheet pdf",
                f"{title} manual pdf",
                f"{mpn} specifications",
            ]
        urls: List[str] = []
        if mpn:
            urls.extend([
                f"https://www.garmin.com/manuals/{mpn}.pdf",
                f"https://static.garmin.com/pumac/{mpn}_OM.pdf",
            ])
        for q in queries[:MAX_SERP_CALLS]:
            urls.extend(get_serp_urls(q))
            time.sleep(0.4)
        seen = set()
        sources = []
        for url in urls:
            if url in seen or len(sources) >= MAX_SOURCES:
                continue
            src = download_and_store(url, temp_dir)
            if src:
                sources.append(src)
                seen.add(url)
        extracted = []
        for src in sources:
            try:
                if src["type"] == "pdf":
                    actual_words_from_pdf = extract_pdf_pdfplumber(src["local_path"])
                    text = extract_from_pdf(actual_words_from_pdf)
                else:
                    html = Path(src["local_path"]).read_text(errors="ignore")
                    text = extract_from_web(html)
                text["source_url"] = src["cloudinary_url"]
                extracted.append(text)
            except Exception as e:
                logger.warning(f"Extraction failed: {e}")
        keys = [k for e in extracted for k in e.get("attributes", {}).keys()]
        mapping = unify_attributes(List(set(keys))) if keys else {
            "canonical_attributes": {}}
        standardized = {}
        for canonical, info in mapping.get("canonical_attributes", {}).items():
            values = []
            for e in extracted:
                for syn in info["synonyms"]:
                    if syn in e.get("attributes", {}):
                        values.append(e["attributes"][syn])
            if values:
                standardized[canonical] = standardize_with_llm(
                    canonical, values)
        golden = build_golden_record(standardized, identifiers)
        return {
            "request_id": request_id,
            "identifiers": identifiers,
            "sources_used": len(sources),
            "golden_record": golden,
            "ready_for_publish": len(standardized) >= 3,
            "status": "success",
        }
