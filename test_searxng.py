import asyncio
from app.search.searxng_service import SearXNGSearchService


async def main():
    service = SearXNGSearchService(
        base_url="http://localhost:8888",
        max_results=5,
    )

    # Test with a real product
    urls = await service.get_urls(
        query="",
        mpn="MP-4506",
        brand="Michigan Pneumatic",
        title="Impact Wrench",
    )

    print(f"\nFound {len(urls)} URLs:\n")
    for i, url in enumerate(urls, 1):
        print(f"  {i}. {url}")


asyncio.run(main())