import logging
from typing import List
from pydantic import BaseModel
from app.aggregation.interfaces import ISearchService
from app.search.searxng_service import SearXNGSearchService
import asyncio
logger = logging.getLogger("smart_search")
class UrlFilterResponse(BaseModel):
    selected_urls: List[str]

class SmartSearchResponse(BaseModel):
    selected_urls: List[str]
    candidate_image_urls: List[str] = []
class SmartSearchService(ISearchService):
    def __init__(
        self,
        searxng_url: str = "http://searxng:8080",
        max_results: int = 5,
    ):
        self.searxng = SearXNGSearchService(
            base_url=searxng_url,
            max_results=15,  
        )
        self.max_results = max_results
    async def get_urls(self, query: str, mpn: str, brand: str, title: str) -> tuple[List[str], List[str]]:
        from app.aggregation.aggregate_product import call_llm_with_schema

        # Run web and image searches concurrently
        web_task = self.searxng._search(self.searxng._build_query(mpn, brand, title))
        image_task = self.searxng.search_images(f"{brand} {mpn} {title}")
        web_results, image_results = await asyncio.gather(web_task, image_task, return_exceptions=True)

        # Handle possible exceptions
        if isinstance(web_results, Exception):
            logger.error(f"Web search failed: {web_results}")
            return [], []
        if isinstance(image_results, Exception):
            logger.warning(f"Image search failed: {image_results}")
            image_results = []

        if not web_results:
            logger.warning(f"SearXNG returned no web results for {mpn}")
            return [], []

        # Format web results
        web_text = "\n".join(
            f"[{i+1}] {r.get('title', 'No title')}\n    URL: {r['url']}\n    Description: {r.get('content', '')[:150]}"
            for i, r in enumerate(web_results[:15])
        )

        # Format image URLs (deduplicate, limit to 10)
        image_urls = list({img.get("img_src") for img in image_results if img.get("img_src")})
        image_text = "\n".join(f"- {url}" for url in image_urls[:10])

        prompt = f"""
    PRODUCT:
    - Brand: {brand}
    - MPN: {mpn}
    - Name: {title}

    WEB SEARCH RESULTS:
    {web_text}

    POSSIBLE PRODUCT IMAGES (from image search):
    {image_text or "None found"}

    TASK:
    Select up to {self.max_results} URLs from the WEB SEARCH RESULTS that are most likely to contain official product specs, datasheets, or pricing.
    Also, if any of the POSSIBLE PRODUCT IMAGES seem to match this product, include them in your output.

    RULES:
    - ONLY select URLs from the list above
    - DO NOT invent or modify any URLs
    - Prefer: manufacturer sites, official distributors, pages that also have a matching image
    - Avoid: unrelated products, forums, blogs

    Return a JSON object with:
    - "selected_urls": list of chosen web URLs
    - "candidate_image_urls": list of image URLs that look correct (max 3)
    """
        try:
            result = await call_llm_with_schema(
                prompt=prompt,
                response_model="SmartSearchResponse",
                estimated_tokens=500,
            )
            if result and result.selected_urls:
                valid_urls = {r["url"] for r in web_results}
                filtered = [u for u in result.selected_urls if u in valid_urls]
                hallucinated = len(result.selected_urls) - len(filtered)
                if hallucinated:
                    logger.warning(f"Removed {hallucinated} hallucinated URLs")
                # Get candidate images from LLM (or fallback to raw image URLs)
                candidate_imgs = result.candidate_image_urls if result.candidate_image_urls else []
                logger.info(f"Smart search for {mpn}: {filtered}")
                return filtered, candidate_imgs
        except Exception as e:
            logger.exception(f"LLM filtering failed: {e}")

        # Fallback: return top web results and top raw image URLs
        return [r["url"] for r in web_results[:self.max_results]], image_urls[:3]