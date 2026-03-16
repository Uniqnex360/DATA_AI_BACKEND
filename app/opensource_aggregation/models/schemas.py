from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from enum import Enum


class SourceType(Enum):
    MANUFACTURER = "manufacturer"
    DISTRIBUTOR = "distributor"
    RETAIL = "retail"
    PDF_MANUAL = "pdf_manual"
    UNKNOWN = "unknown"


@dataclass
class ProductIdentifier:
    """Input product identifiers"""
    mpn: str
    brand: str = ""
    title: str = ""
    upc: str = ""
    model_number: str = ""


@dataclass
class ExtractedAttribute:
    """Single attribute extracted from a source"""
    name: str
    value: str
    unit: Optional[str] = None
    confidence: float = 0.5
    source_url: str = ""
    source_type: SourceType = SourceType.UNKNOWN


@dataclass
class SourceResult:
    """Result from extracting one source"""
    url: str
    source_type: SourceType
    attributes: List[ExtractedAttribute] = field(default_factory=list)
    image_url: Optional[str] = None
    extraction_method: str = "html_table"
    success: bool = True
    error: Optional[str] = None


@dataclass
class UnifiedAttribute:
    """Attribute after name unification"""
    canonical_name: str
    values: List[ExtractedAttribute] = field(default_factory=list)

    @property
    def best_value(self) -> Optional[str]:
        """Get highest confidence value"""
        if not self.values:
            return None
        return max(self.values, key=lambda x: x.confidence).value

    @property
    def consensus_value(self) -> Optional[str]:
        """Get most common value"""
        if not self.values:
            return None
        from collections import Counter
        counts = Counter(v.value.lower().strip() for v in self.values)
        return counts.most_common(1)[0][0]


@dataclass
class GoldenRecord:
    """Final canonical product record"""
    brand: str
    mpn: str
    title: str
    category: str = ""
    attributes: Dict[str, Any] = field(default_factory=dict)
    image_url: Optional[str] = None
    sources_consulted: List[str] = field(default_factory=list)
    confidence_score: float = 0.0
    conflicts: Dict[str, List[str]] = field(default_factory=dict)