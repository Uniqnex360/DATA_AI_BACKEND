import logging
from typing import List, Optional
from urllib.parse import urlparse
from pydantic import BaseModel, Field
from app.llm import call_llm_with_schema
import httpx

logger = logging.getLogger("llm_url_finder")




class LLMUrlFinderService:
    def __init__(self, llm_provider: str = 'openai'):
        self.llm_provider = llm_provider

    async def find_correct_pdp(
        self,
        brand: str,
        title: str,
        mpn: str,
        search_results: List[dict]
    ) -> Optional[str]:
        """
        Passes search results to LLM to pick the exact product page.
        """
        if not search_results:
            return None

        # Prepare the list of candidates for the LLM
        candidates = []
        for i, res in enumerate(search_results[:15]):  # Top 15 results
            candidates.append({
                "id": i,
                "title": res.get("title"),
                "url": res.get("url") or res.get("link"),
                "snippet": res.get("content") or res.get("snippet")
            })

        prompt = f"""
        You are a Senior Product Data Researcher. Your task is to identify the EXACT Product Detail Page (PDP) for a specific product.
        
        PRODUCT WE ARE LOOKING FOR:
        - Brand: {brand}
        - Title: {title}
        - MPN/ID: {mpn}

        CANDIDATE SEARCH RESULTS:
        {candidates}

        STRICT RULES:
        1. Pick the URL that leads to the specific product page for THIS product.
        2. REJECT Category pages (e.g., results showing "List of Dog Toys" or "Pet Accessories Category").
        3. REJECT Brand homepages.
        4. REJECT Variant mismatches (e.g., if we want a 'Medium' size and the link says 'Large').
        5. For numeric IDs like '{mpn}', be extra careful. Only pick the link if the Title and Snippet strongly match the product name.
        6. Prefer URLs from the official brand site ({brand}) or major reputable retailers (Chewy, Petco, Zooplus, etc.).
        7. If NO URL is a perfect match for the specific product, return null for best_url.

        Return a JSON object with: "best_url", "confidence", and "reasoning".
        """

        try:
            result = await call_llm_with_schema(
                prompt=prompt,
                response_model="URLSelectionResponse",
                llm_provider=self.llm_provider,
                estimated_tokens=1000
            )

            if result and result.best_url and result.confidence >= 0.8:
                logger.info(
                    f"✓ LLM selected URL with {result.confidence} confidence: {result.best_url}")
                return result.best_url

            logger.warning(
                f"LLM could not find a high-confidence match. Best guess was: {result.best_url if result else 'None'}")
            return None

        except Exception as e:
            logger.error(f"Error in LLM URL selection: {e}")
            return None
