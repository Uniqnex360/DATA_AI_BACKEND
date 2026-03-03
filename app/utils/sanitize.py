from typing import Any
def sanitize_ai_data(data: Any) -> Any:
    if isinstance(data, str):
        return data.replace('\u0000', '').replace('\x00', '').strip()
    if isinstance(data, dict):
        return {k: sanitize_ai_data(v) for k, v in data.items()}
    if isinstance(data, list):
        return [sanitize_ai_data(i) for i in data]
    return data