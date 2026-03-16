from dataclasses import dataclass, field
from typing import Dict


@dataclass
class AggregationConfig:
    """Configuration for open-source aggregation"""

    # Search settings
    max_search_results: int = 10
    max_sources_to_extract: int = 5
    search_timeout: int = 10

    # Extraction settings
    download_timeout: int = 30
    max_html_size: int = 2_000_000  # 2MB
    max_pdf_pages: int = 10

    # Unification settings
    embedding_model: str = "all-MiniLM-L6-v2"
    similarity_threshold: float = 0.80  # Consider same if > 80% similar

    # Confidence weights by source type
    source_confidence: Dict[str, float] = field(default_factory=lambda: {
        "manufacturer": 1.0,
        "pdf_manual": 0.95,
        "distributor": 0.85,
        "retail": 0.75,
        "unknown": 0.5
    })

    # Known manufacturer domains
    manufacturer_domains: list = field(default_factory=lambda: [
        "garmin.com", "3m.com", "honeywell.com", "bosch.com",
        "siemens.com", "schneider-electric.com", "aervoe.com",
        "michiganpneumatic.com", "aymcdonald.com", "buntingbearings.com"
    ])

    # Known distributor domains
    distributor_domains: list = field(default_factory=lambda: [
        "grainger.com", "mcmaster.com", "digikey.com", "mouser.com",
        "fastenal.com", "applied.com", "wesco.com"
    ])

    # Known retail domains
    retail_domains: list = field(default_factory=lambda: [
        "amazon.com", "homedepot.com", "lowes.com", "walmart.com"
    ])


config = AggregationConfig()