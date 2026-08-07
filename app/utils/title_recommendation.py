import json
from typing import Dict, Any

from app.llm import call_llm_with_schema
MEASUREMENT_ABBREVIATIONS = {
    "length": "L",
    "width": "W",
    "depth": "D",
    "height": "H",
    "diameter": "Dia",
}


def abbreviate_measurement(attr_name: str) -> str:
    name_lower = attr_name.strip().lower()
    if name_lower in MEASUREMENT_ABBREVIATIONS:
        return MEASUREMENT_ABBREVIATIONS[name_lower]
    return attr_name

async def generate_title_recommendation(
    brand: str,
    attributes: Dict[str, Any],
    taxonomy: str,
    llm_provider: str,
):
    # Build a spec list with real values
    spec_lines = []
    for name, a in (attributes or {}).items():
        if not a:
            continue
        value = a.get("value")
        unit = a.get("unit")
        conf = a.get("confidence")

        if value is None or str(value).strip() == "":
            continue
        display_name=abbreviate_measurement(name)
        line = f"- {display_name}: {value}" + (f" {unit}" if unit else "")
        if conf is not None:
            line += f" (confidence={conf})"
        spec_lines.append(line)

    specs_text = "\n".join(spec_lines[:40])  # include enough real specs

    prompt = f"""
You generate standardized e-commerce product titles.

FORMAT (in order):
Brand + Sub Brand/Series (optional) + Product Type + Key Specs (max 3)

INPUT:
- Brand: {brand}
- Category/Taxonomy: {taxonomy}

VERIFIED SPECS (ONLY use these; do not invent anything):
{specs_text}

RULES:
- Do NOT include MPN/SKU/UPC/GTIN in the title.
- Do NOT invent Sub Brand/Series or Product Type. If not clearly present in verified specs or taxonomy, omit it.
- Choose up to 3 key specs that best differentiate variants (e.g., Voltage, Capacity, Size, Material, Color, Wattage).
- Keep title <= 140 characters.
- Use Title Case words; keep units as provided.

Return JSON exactly:
{{
  "recommended_title": "...",
  "confidence": 0.0
}}
""".strip()

    result = await call_llm_with_schema(
        prompt=prompt,
        response_model="TitleRecommendationResponse",
        llm_provider=llm_provider,
        estimated_tokens=600,
        max_tokens=300,
    )
    return result