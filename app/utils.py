import re
INVALID_VALUES = {"n/a", "-", "unknown", "none", "", "not specified", "tbd"}
def is_invalid(value:str)->bool:
    return value.strip().lower() in INVALID_VALUES or value.strip()==''
def normalize_text(value:str)->str:
    return re.sub(r"\s+", " ", value.strip())
def extract_number(value: str):
    match = re.search(r"(\d+(\.\d+)?)", value)
    return float(match.group(1)) if match else None