import asyncio
from app.opensource_aggregation import aggregate_product


async def main():
    # Test with a real product
    result = await aggregate_product(
        mpn="5133-116",
        brand="A.Y. McDonald",
        title="Ball Corporation Stop",
        serp_api_key="YOUR_SERP_KEY"  # Optional
    )

    print("\n" + "=" * 60)
    print(" GOLDEN RECORD")
    print("=" * 60)
    print(f"Brand: {result.brand}")
    print(f"MPN: {result.mpn}")
    print(f"Confidence: {result.confidence_score:.2f}")
    print(f"Image: {result.image_url}")
    print(f"\nAttributes ({len(result.attributes)}):")

    for name, value in result.attributes.items():
        print(f"  {name}: {value}")

    if result.conflicts:
        print(f"\n Conflicts ({len(result.conflicts)}):")
        for name, values in result.conflicts.items():
            print(f"  {name}:")
            for v in values:
                print(f"    - {v}")

    print(f"\nSources ({len(result.sources_consulted)}):")
    for url in result.sources_consulted:
        print(f"  - {url}")


if __name__ == "__main__":
    asyncio.run(main())