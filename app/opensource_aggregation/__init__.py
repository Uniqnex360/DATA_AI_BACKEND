"""
Open-Source Product Aggregation

No OpenAI. No API costs. Runs locally.

Usage:
    from app.opensource_aggregation import aggregate_product

    result = await aggregate_product(
        mpn="GPSMAP 923",
        brand="Garmin",
        title="Garmin Marine Chartplotter"
    )

    print(result.attributes)
"""

from app.opensource_aggregation.pipeline import OpenSourceAggregationPipeline
from app.opensource_aggregation.models.schemas import ProductIdentifier, GoldenRecord
from app.opensource_aggregation.config import config


async def aggregate_product(
    mpn: str,
    brand: str = "",
    title: str = "",
    serp_api_key: str = None
) -> GoldenRecord:
    """
    Simple function to aggregate a product

    Args:
        mpn: Product MPN/model number
        brand: Product brand
        title: Product title
        serp_api_key: Optional SerpAPI key for web search

    Returns:
        GoldenRecord with aggregated attributes
    """
    pipeline = OpenSourceAggregationPipeline(serp_api_key=serp_api_key)

    try:
        product = ProductIdentifier(
            mpn=mpn,
            brand=brand,
            title=title
        )
        return await pipeline.aggregate(product)
    finally:
        await pipeline.close()