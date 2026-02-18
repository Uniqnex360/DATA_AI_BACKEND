import asyncio
from playwright.async_api import async_playwright

async def run():
    try:
        async with async_playwright() as p:
            print("Starting browser...")
            browser = await p.chromium.launch(headless=True)
            print("✅ SUCCESS: Browser launched and ready for AI scraping.")
            await browser.close()
    except Exception as e:
        print(f"❌ FAILED: {str(e)}")

asyncio.run(run())