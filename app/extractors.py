import logging
from typing import Optional, List, Dict
import fitz
import pdfplumber
import pandas as pd
import cv2
import pytesseract
import requests
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from pathlib import Path
import httpx
from bs4 import BeautifulSoup
import asyncio
MAX_PDF_MB = 100
MAX_IMAGE_MB = 10
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def extract_web(url: str):
    try:
        resp = httpx.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"},verify=False)
        if resp.status_code == 200 and len(resp.text) > 1000:
            soup = BeautifulSoup(resp.text, "html.parser")
            for s in soup(["script", "style", "nav", "footer", "header", "svg", "noscript", "iframe"]):
                s.decompose()
            important_tags = soup.find_all(['table', 'dl', 'ul', 'main', 'article'])
            cleaned_text = " ".join([tag.get_text(separator=' ', strip=True) for tag in important_tags])
            if len(cleaned_text) < 500:
                cleaned_text = soup.body.get_text(separator=' ', strip=True)
            # content = soup.find('main') or soup.find('body')
            # return content.get_text(separator=' ', strip=True)
            return cleaned_text
    except:
        pass

    return extract_web_playwright(url)
import asyncio
import logging
from typing import Optional
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger("extractor")
async def extract_web_playwright(url: str, timeout: int = 30_000) -> Optional[str]:
    browser = None
    try:
        async with async_playwright() as p:
            # 1. ADD STEALTH ARGS: This hides the 'Automation' flag from websites
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox"
                ]
            )
            
            # 2. Add extra headers to look like a real browser
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                ignore_https_errors=True,
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"}
            )
            
            page = await context.new_page() 
            
            # 3. Increase wait time for spec tables to render
            await page.goto(url, timeout=timeout, wait_until="networkidle")
            await asyncio.sleep(5) # Allow dynamic price/specs to pop in
            
            content = await page.content()
            return content
            
    except Exception as e:
        logger.error("Playwright failed on %s: %s", url, str(e))
        return None
    finally:
        if browser: await browser.close()
        
def extract_pdf_pdfplumber(path: str) -> str:
    file = Path(path)
    if not file.exists():
        logger.warning("PDF not found", extra={"path": path})
        return ''
    if file.stat().st_size > MAX_PDF_MB*1024*1024:
        logger.warning("PDF too large", extra={"path": path})
        return ''
    try:
        with pdfplumber.open(path) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as e:
        logger.error(f"pdfplumber failed on {path}: {e}")
        return extract_pdf_pymupdf(path)


def extract_pdf_pymupdf(path: str) -> str:
    try:
        doc = fitz.open(path)
        text = '\n'.join(page.get_text('text') for page in doc)
        doc.close()
        return text
    except Exception as e:
        logger.error(f"PYMuPDF failed on {path}:{e}")
        return ""


def extract_csv_excel(path: str) -> List[Dict]:
    file = Path(path)
    if not file.exists():
        logger.warning("CSV/Excel not found", extra={'path': path})
        return []
    try:
        if path.endswith('.csv'):
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file)
        return df.to_dict(orient='records')
    except Exception as e:
        logger.error(f"Pandas failed on {path}:{e}")
        return []


def extract_image_text(path: str) -> List[Dict]:
    file = Path(path)
    if not file.exists():
        logger.warning("Image not found", extra={'path': path})
        return []
    if file.stat().st_size > MAX_IMAGE_MB*1024*1024:
        logger.warning("Image too large for OCR", extra={'path': path})
        return []
    try:
        img = cv2.imread(path)
        if img is None:
            logger.warning(f"OpenCV couldn't read image :{path}")
            return []
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        text = pytesseract.image_to_string(gray, lang='eng', config='--psm 6')
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        return lines
    except Exception as e:
        logger.error(f"OCR failed on {path}:{e}")
        return []
