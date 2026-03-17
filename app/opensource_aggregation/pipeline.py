import asyncio
import logging
from typing import Optional
from app.opensource_aggregation.config import config, AggregationConfig
from app.opensource_aggregation.models.schemas import (
    ProductIdentifier, GoldenRecord, SourceType
)
from app.opensource_aggregation.discovery.search import SourceDiscovery
from app.opensource_aggregation.extraction.html_extractor import HtmlExtractor
from app.opensource_aggregation.extraction.pdf_extractor import PdfExtractor
from app.opensource_aggregation.unification.semantic_matcher import SemanticMatcher
from app.opensource_aggregation.aggregation.conflict_resolver import ConflictResolver
logger = logging.getLogger("os_pipeline")


class OpenSourceAggregationPipeline:
    def __init__(
        self,
        serp_api_key: Optional[str] = None,
        pipeline_config: Optional[AggregationConfig] = None
    ):
        self.config = pipeline_config or config
        self.discovery = SourceDiscovery(serp_api_key=serp_api_key)
        self.html_extractor = HtmlExtractor()
        self.pdf_extractor = PdfExtractor()
        self.semantic_matcher = SemanticMatcher()
        self.conflict_resolver = ConflictResolver()

    async def aggregate(self, product: ProductIdentifier) -> GoldenRecord:
        logger.info(f" Starting aggregation for {product.brand} {product.mpn}")
        logger.info("Stage 1: Discovering sources...")
        sources = await self.discovery.discover_sources(product)
        if not sources:
            logger.warning(f"No sources found for {product.mpn}")
            return GoldenRecord(
                brand=product.brand,
                mpn=product.mpn,
                title=product.title,
                confidence_score=0.0
            )
        logger.info(f"   Found {len(sources)} sources")
        logger.info(" Stage 2: Extracting attributes...")
        all_attributes = []
        all_sources = []
        best_image = None
        for source in sources:
            url = source['url']
            source_type = source['source_type']
            if url.lower().endswith('.pdf'):
                result = await self.pdf_extractor.extract(url)
            else:
                result = await self.html_extractor.extract(url, source_type)
            if result.success:
                all_attributes.extend(result.attributes)
                all_sources.append(url)
                if not best_image and result.image_url:
                    best_image = result.image_url
            await asyncio.sleep(1)
        logger.info(
            f"   Extracted {len(all_attributes)} total attributes from {len(all_sources)} sources")
        if not all_attributes:
            logger.warning(f"No attributes extracted for {product.mpn}")
            return GoldenRecord(
                brand=product.brand,
                mpn=product.mpn,
                title=product.title,
                sources_consulted=all_sources,
                confidence_score=0.0
            )
        logger.info(" Stage 3: Unifying attribute names...")
        unified = self.semantic_matcher.unify_attributes(all_attributes)
        logger.info(f"   Unified into {len(unified)} canonical attributes")
        logger.info("⚖️ Stage 4: Resolving conflicts...")
        golden = self.conflict_resolver.resolve(
            product=product,
            unified_attributes=unified,
            sources_consulted=all_sources,
            image_url=best_image
        )
        logger.info(f"Aggregation complete for {product.mpn}")
        logger.info(f"   Attributes: {len(golden.attributes)}")
        logger.info(f"   Confidence: {golden.confidence_score:.2f}")
        logger.info(f"   Conflicts: {len(golden.conflicts)}")
        return golden

    async def close(self):
        """Cleanup resources"""
        await self.discovery.close()
        await self.html_extractor.close()
        await self.pdf_extractor.close()
