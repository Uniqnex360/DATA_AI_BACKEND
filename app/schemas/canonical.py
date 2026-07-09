from pydantic import BaseModel
from typing import List

class AliasDecision(BaseModel):
    aliases: List[str]
    preferred: str
    confidence: float
    reason: str

class CanonicalAliasResponse(BaseModel):
    decisions: List[AliasDecision]