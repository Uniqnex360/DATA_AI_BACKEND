import logging
from typing import Optional, List, Dict
import fitz  
import pdfplumber
import pandas as pd
import cv2
import pytesseract
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from pathlib import Path
import httpx
from bs4 import BeautifulSoup

MAX_PDF_MB=100
MAX_IMAGE_MB=10                                                                                     
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
def extract_web(url: str):
    try:
        resp = httpx.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200 and len(resp.text) > 1000:
            soup = BeautifulSoup(resp.text, "html.parser")
            for s in soup(["script", "style", "nav", "footer", "header"]):
                s.decompose()
            return soup.get_text()
    except:
        pass

    return extract_web_playwright(url) 

def extract_web_playwright(url: str, timeout: int = 30_000) -> Optional[str]:
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (compatible; DataAggregationBot/1.0)"
            )
            page = context.new_page()
            page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            page.wait_for_timeout(2000) 
            content = page.content()
            browser.close()
            return content
    except PlaywrightTimeout:
        logger.warning(f"Timeout loading {url}")
    except Exception as e:
        logger.error(f"Playwright failed on {url}: {e}")
    return None

def extract_pdf_pdfplumber(path: str) -> str:
    file=Path(path)
    if not file.exists():
        logger.warning("PDF not found",extra={"path":path})
        return ''
    if  file.stat().st_size>MAX_PDF_MB*1024*1024:
        logger.warning("PDF too large",extra={"path":path})
        return ''
    try:
        with pdfplumber.open(path) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as e:
        logger.error(f"pdfplumber failed on {path}: {e}")
        return extract_pdf_pymupdf(path) 

def extract_pdf_pymupdf(path:str)->str:
    try:
        doc=fitz.open(path)
        text='\n'.join(page.get_text('text') for page in doc)
        doc.close()
        return text
    except Exception as e:
        logger.error(f"PYMuPDF failed on {path}:{e}")
        return ""
    
def extract_csv_excel(path:str)->List[Dict]:
    file=Path(path)
    if not file.exists():
        logger.warning("CSV/Excel not found",extra={'path':path})
        return []
    try:
        if path.endswith('.csv'):
            df=pd.read_csv(file)
        else:
            df=pd.read_excel(file)
        return df.to_dict(orient='records')
    except Exception as e:
        logger.error(f"Pandas failed on {path}:{e}")
        return []

def extract_image_text(path:str)->List[Dict]:
    file=Path(path)
    if not file.exists():
        logger.warning("Image not found",extra={'path':path})
        return []
    if file.stat().st_size>MAX_IMAGE_MB*1024*1024:
        logger.warning("Image too large for OCR",extra={'path':path})
        return []
    try:
        img=cv2.imread(path)
        if img is None:
            logger.warning(f"OpenCV couldn't read image :{path}")
            return []
        gray=cv2.cvtColor(img,cv2.COLOR_BGR2GRAY)
        text=pytesseract.image_to_string(gray,lang='eng',config='--psm 6')
        lines=[line.strip() for line in text.split('\n') if line.strip()]
        return lines
    except Exception as e:
        logger.error(f"OCR failed on {path}:{e}")
        return []

