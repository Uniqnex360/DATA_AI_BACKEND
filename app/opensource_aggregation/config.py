from dataclasses import dataclass, field
from typing import Dict
@dataclass
class AggregationConfig:
    """Configuration for open-source aggregation"""
    max_search_results: int = 10
    max_sources_to_extract: int = 5
    search_timeout: int = 10
    download_timeout: int = 30
    max_html_size: int = 2_000_000  
    max_pdf_pages: int = 10
    embedding_model: str = "all-MiniLM-L6-v2"
    similarity_threshold: float = 0.80  
    source_confidence: Dict[str, float] = field(default_factory=lambda: {
        "manufacturer": 1.0,
        "pdf_manual": 0.95,
        "distributor": 0.85,
        "retail": 0.75,
        "unknown": 0.5
    })
    manufacturer_domains: list = field(default_factory=lambda: [
        "garmin.com", "3m.com", "honeywell.com", "bosch.com",
        "siemens.com", "schneider-electric.com", "aervoe.com",
        "michiganpneumatic.com", "aymcdonald.com", "buntingbearings.com"
    ])
    distributor_domains: list = field(default_factory=lambda: [
        "grainger.com", "mcmaster.com", "digikey.com", "mouser.com",
        "fastenal.com", "applied.com", "wesco.com"
    ])
    retail_domains: list = field(default_factory=lambda: [
        "amazon.com", "homedepot.com", "lowes.com", "walmart.com"
    ])
config = AggregationConfig()