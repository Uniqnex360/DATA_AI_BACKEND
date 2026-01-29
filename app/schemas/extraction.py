from pydantic import BaseModel
from typing import Optional

class ExtractionRequest(BaseModel):
    sourceType: str  
    content: str     
    sourceUrl: str   