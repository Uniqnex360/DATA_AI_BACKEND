import re
import pdfplumber
from io import BytesIO
def extract_pdf_text(pdf_bytes: bytes, max_pages: int = 60) -> str:
    full_text = ""
    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages[:max_pages]:
                try:
                    page_text = page.extract_text(
                        layout=True) or page.extract_text() or ""
                    if page_text:
                        full_text += page_text + "\n"
                    tables = page.extract_tables() or []
                    for tbl in tables:
                        for row in tbl or []:
                            row_text = " ".join(
                                str(cell).strip()
                                for cell in (row or [])
                                if cell is not None and str(cell).strip()
                            )
                            if row_text:
                                full_text += row_text + "\n"
                except Exception:
                    continue
    except Exception as e:
        print(f"Error reading PDF: {e}")
        return ""
    return full_text
def score_pdf_text_for_mpn(text: str, filename: str, mpn: str) -> int:
    try:
        if not text:
            return 0
        text_lower = text.lower()
        mpn_clean = str(mpn).lower().replace(".0", "")
        digits = re.sub(r"\D+", "", mpn_clean)
        score = 0
        if digits:
            spaced_pat = r"\s*".join(map(re.escape, digits))
            if re.search(spaced_pat, text_lower):
                score += 100
        for part in mpn_clean.replace("-", " ").replace("_", " ").split():
            if len(part) > 2 and part in text_lower:
                score += 20
        if mpn_clean and mpn_clean in (filename or "").lower():
            score += 50
        return score
    except Exception as e:
        print(f"Error scoring PDF text for MPN '{mpn}': {e}")
        return 0
def slice_text_around_mpn(text: str, mpn: str, window: int = 15000, back: int = 6000) -> str:
    try:
        if not text:
            return ""
        mpn_clean = str(mpn).lower().replace(".0", "")
        digits = re.sub(r"\D+", "", mpn_clean)
        truncated = text[:window]
        if digits:
            spaced_pat = r"\s*".join(map(re.escape, digits))
            m = re.search(spaced_pat, text.lower())
            if m:
                start = max(0, m.start() - back)
                truncated = text[start : start + window]
        return truncated
    except Exception as e:
        print(f"Error slicing text for MPN '{mpn}': {e}")
        return text[:window] if text else ""

PDF_ONLY_SAFETY_INSTRUCTIONS = """
CRITICAL INSTRUCTIONS:
- ONLY use information explicitly present in the document content
- DO NOT use any external knowledge or information from outside this document
- DO NOT search the internet or use prior knowledge about the product or brand
- If information is not found in the document, leave the field empty
- DO NOT guess or fabricate any values
"""