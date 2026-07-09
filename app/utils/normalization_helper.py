# from typing import Dict, List
# import re

# def normalize_concatenated_uom(attrs: List[Dict]) -> List[Dict]:
#     uom_pattern = r'^([\d\.]+)\s*([a-zA-Z]+)$'
#     for attr in attrs:
#         if not attr.get('unit') and isinstance(attr.get('value'), str):
#             match = re.match(uom_pattern, attr['value'].strip())
#             if match:
#                 val, uom = match.groups()
#                 attr['value'] = val
#                 attr['unit'] = uom.lower() 
#     return attrs
from typing import Dict, List
import re

def normalize_concatenated_uom(attrs: List[Dict]) -> List[Dict]:
    """
    1. EXISTING: Splits concatenated values like "76ft" -> value="76", unit="ft"
    2. NEW: Standardizes units using UOM mapping (optional, non-breaking)
    """
    # ============================================
    # EXISTING FUNCTIONALITY (unchanged)
    # ============================================
    uom_pattern = r'^([\d\.]+)\s*([a-zA-Z]+)$'
    for attr in attrs:
        if not attr.get('unit') and isinstance(attr.get('value'), str):
            match = re.match(uom_pattern, attr['value'].strip())
            if match:
                val, uom = match.groups()
                attr['value'] = val
                attr['unit'] = uom.lower()  # <-- Original: lowercase
    
    # ============================================
    # NEW: UOM Standardization (additive, non-breaking)
    # ============================================
    attrs = _standardize_uom_in_attrs(attrs)
    
    return attrs


def _standardize_uom_in_attrs(attrs: List[Dict]) -> List[Dict]:
    """
    Standardize units without changing existing behavior.
    Only applies to attributes that already have a unit field.
    """
    for attr in attrs:
        if not attr.get('unit') or not isinstance(attr.get('unit'), str):
            continue
        
        unit = attr['unit'].strip()
        if not unit:
            continue
        
        # Handle compound units (e.g., "ft, in", "in x ft")
        if ',' in unit:
            parts = [p.strip() for p in unit.split(',')]
            parts = [_standardize_single_uom(p) for p in parts]
            attr['unit'] = ', '.join(parts)
        elif re.search(r'\s+x\s+', unit, re.IGNORECASE):
            parts = re.split(r'\s+x\s+', unit, flags=re.IGNORECASE)
            parts = [_standardize_single_uom(p) for p in parts]
            attr['unit'] = ' x '.join(parts)
        else:
            # Single unit - apply standardization
            attr['unit'] = _standardize_single_uom(unit)
    
    return attrs


def _standardize_single_uom(unit_str: str) -> str:
    """
    Standardize a single UOM string. Returns original if no match.
    """
    if not unit_str:
        return unit_str
    
    cleaned = unit_str.strip()
    
    # Length / Distance
    if cleaned.lower() in ['inch', 'inches', 'in.']:
        return 'in'
    if cleaned.lower() in ['foot', 'feet', 'ft', 'ft.']:
        return 'ft'
    if cleaned.lower() in ['yard', 'yards', 'yd', 'yd.']:
        return 'yd'
    if cleaned.lower() in ['millimeter', 'millimeters']:
        return 'mm'
    if cleaned.lower() in ['centimeter', 'centimeters']:
        return 'cm'
    if cleaned.lower() in ['meter', 'meters']:
        return 'm'
    
    # Volume
    if cleaned.lower() in ['gallon', 'gallons', 'gal', 'gal.']:
        return 'gal'
    if cleaned.lower() in ['liter', 'liters']:
        return 'L'
    if cleaned.lower() in ['milliliter', 'milliliters']:
        return 'mL'
    
    # Weight
    if cleaned.lower() in ['pound', 'pounds', 'lb', 'lbs', 'lb.']:
        return 'lb'
    if cleaned.lower() in ['ounce', 'ounces', 'oz', 'oz.']:
        return 'oz'
    if cleaned.lower() in ['kilogram', 'kilograms']:
        return 'kg'
    if cleaned.lower() in ['gram', 'grams']:
        return 'g'
    
    # Electrical
    if cleaned.lower() in ['volt', 'volts']:
        return 'V'
    if cleaned.lower() in ['amp', 'amps', 'ampere', 'amperes']:
        return 'A'
    if cleaned.lower() in ['watt', 'watts']:
        return 'W'
    if cleaned.lower() in ['millivolt', 'millivolts']:
        return 'mV'
    
    # Temperature
    if cleaned.lower() in ['degrees celsius', 'celsius', '°c', 'deg c', 'degrees c', 'deg. c']:
        return 'deg C'
    if cleaned.lower() in ['degrees fahrenheit', 'fahrenheit', '°f', 'deg f', 'degrees f', 'deg. f']:
        return 'deg F'
    if cleaned.lower() in ['deg', 'degree', 'degrees', '°', 'deg.']:
        return 'deg'

    # Count / Pack
    if cleaned.lower() in ['pk', 'pack', 'packs', 'pkg', 'pcs', 'pc', 'ea', 'each', 'unit', 'units']:
        return 'pc'
    
    # Special (preserve original casing if already correct)
    if cleaned.lower() == 'tpi':
        return 'TPI'
    if cleaned.lower() == 'mil':
        return 'mil'
    if cleaned.lower() == 'rpm':
        return 'RPM'
    if cleaned.lower() == 'mah':
        return 'mAh'
    if cleaned.lower() == 'hp':
        return 'hp'
    
    # Pressure
    if cleaned.lower() == 'psi':
        return 'psi'
    if cleaned.lower() == 'bar':
        return 'bar'
    
    # Return original if no match (preserves unknown units)
    return cleaned