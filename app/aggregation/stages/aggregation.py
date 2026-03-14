from enum import Enum
import logging
logger=logging.getLogger('aggregate_attributes')
from typing import Dict,List
class SourceType(Enum):
    MANUFACTURER_PDF = 1.00
    MANUFACTURER_WEBSITE =1.00
    OFFICIAL_DATASHEET = 0.95
    DISTRIBUTOR_OFFICIAL = 0.85
    RETAILER_AUTHORIZED = 0.75
    RETAILER_GENERIC = 0.65
    FORUM_VERIFIED = 0.50
    FORUM_UNVERIFIED = 0.30

def classify_source(url: str, domain: str) -> float:
    
    url_lower = url.lower()
    domain_lower = domain.lower()
    
    
    if url_lower.endswith('.pdf'):
        if any(indicator in domain_lower for indicator in ['manufacturer', 'official', 'cdn']):
            return SourceType.MANUFACTURER_PDF.value
        return SourceType.OFFICIAL_DATASHEET.value
    
    
    manufacturer_indicators = ['mfg', 'manufacturing', 'industrial', 'corp', 'inc']
    if any(ind in domain_lower for ind in manufacturer_indicators):
        return SourceType.MANUFACTURER_WEBSITE.value
    
    
    distributors = ['digikey', 'mouser', 'newark', 'arrow', 'avnet', 'farnell']
    if any(dist in domain_lower for dist in distributors):
        return SourceType.DISTRIBUTOR_OFFICIAL.value
    
    
    retailers = ['amazon', 'ebay', 'walmart', 'homedepot', 'lowes']
    if any(ret in domain_lower for ret in retailers):
        return SourceType.RETAILER_AUTHORIZED.value
    
    
    return SourceType.RETAILER_GENERIC.value

async def aggregate_attributes(
    sources_data: List[Dict],
    primary_attributes: List[str]
) -> Dict:
    """
    Aggregate attributes from multiple sources using trust scoring
    
    Algorithm:
    1. Group by attribute name
    2. For each group, rank sources by trust
    3. Use highest-trust value if confidence > threshold
    4. If conflict between high-trust sources, flag for review
    """
    attribute_groups = {}
    
    # Group all values by attribute name
    for source in sources_data:
        source_trust = classify_source(source['url'], source['domain'])
        
        for attr in source['attributes']:
            attr_name = attr['name']
            if attr_name not in attribute_groups:
                attribute_groups[attr_name] = []
            
            attribute_groups[attr_name].append({
                'value': attr['value'],
                'unit': attr.get('unit'),
                'confidence': attr['confidence'],
                'source_trust': source_trust,
                'source_url': source['url'],
                'combined_score': attr['confidence'] * source_trust
            })
    
    # Aggregate each attribute
    golden_attributes = []
    
    for attr_name, values in attribute_groups.items():
        # Sort by combined score (confidence × trust)
        values_sorted = sorted(values, key=lambda x: x['combined_score'], reverse=True)
        
        # Check for consensus
        top_value = values_sorted[0]
        agreeing_sources = [v for v in values_sorted if v['value'] == top_value['value']]
        
        golden_attributes.append({
            'name': attr_name,
            'value': top_value['value'],
            'unit': top_value['unit'],
            'confidence': top_value['confidence'],
            'source_count': len(agreeing_sources),
            'sources': [v['source_url'] for v in agreeing_sources[:3]],  # Top 3
            'trust_score': top_value['source_trust'],
            'has_conflict': len(set(v['value'] for v in values_sorted)) > 1
        })
    
    return {
        'golden_attributes': golden_attributes,
        'total_sources': len(sources_data),
        'consensus_rate': sum(1 for attr in golden_attributes if attr['source_count'] > 1) / len(golden_attributes) if golden_attributes else 0
    }