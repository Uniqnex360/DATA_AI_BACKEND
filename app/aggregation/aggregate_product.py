
from typing import Dict
from app.aggregation.pipeline import AggregationPipeline
from app.aggregation.services.search_service import SerpApiSearchService
from app.aggregation.services.download_service import HttpDownloadService
from app.aggregation.services.extraction_service import (
    ExtractionService, HtmlExtractor, PdfExtractor, PlaywrightExtractor,StructuredDataExtractor
)
from app.aggregation.services.image_service import ImageService


def build_pipeline() -> AggregationPipeline:
    return AggregationPipeline(
        search_service=SerpApiSearchService(max_results=5),
        download_service=HttpDownloadService(timeout=30),
        extraction_service=ExtractionService(extractors=[
            HtmlExtractor(),
            PdfExtractor(),
            PlaywrightExtractor(),     
            PdfExtractor(),  
        ]),
        image_service=ImageService(),
    )


async def aggregate_product(
    mpn: str = None,
    upc: str = None,
    title: str = None
) -> Dict:
    pipeline = build_pipeline()
    return await pipeline.run(mpn=mpn, upc=upc, title=title)