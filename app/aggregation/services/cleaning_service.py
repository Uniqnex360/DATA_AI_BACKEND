import asyncio
import logging
from re import S
from typing import List, Optional, Dict
from pydantic import AliasChoices, BaseModel, Field
from app import llm
from app.core.rate_limiter import openai_limiter
from app.rules.rule_engine import RuleEngine
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.cleaning import AttributeInput, CleanedAttribute, LLMCleaningResponse, ProductContext

logger = logging.getLogger('cleaning_service')


class LLMCleaningService:

    def __init__(
        self,
        llm_provider: str,
        db: Optional[AsyncSession] = None,
        project_id: Optional[str] = None,
        model: str = "gpt-4o-mini",
        max_retries: int = 3,
        concurrency_limit: int = 10
    ):
        self.llm_provider = llm_provider
        self.db = db
        self.project_id = project_id
        self.model = model
        self.max_retries = max_retries
        self._semaphore = asyncio.Semaphore(concurrency_limit)
        self.rule_engine = RuleEngine(db) if db else None

    async def _get_dynamic_prompt(
        self,
        attributes: List[AttributeInput],
        context: ProductContext
    ) -> Optional[str]:
        if not self.rule_engine:
            return None
        try:
            attr_lines = []
            for attr in attributes:
                line = f"ID: {attr.id}\n  Name: {attr.name}\n  Value: {attr.value}"
                if attr.unit:
                    line += f"\n  Unit: {attr.unit}"
                attr_lines.append(line)
            attributes_text = "\n\n".join(attr_lines)
            rule_context = {
                "mpn": context.mpn,
                "brand": context.brand,
                "product_name": context.product_name,
                "taxonomy": context.taxonomy,
                "attribute_count": len(attributes),
                "attributes_text": attributes_text,
                "attributes": [attr.dict() for attr in attributes]
            }
            return await self.rule_engine.get_active_prompt(
                stage="cleaning",
                operation_mode="cleaning",
                use_case="Data cleaning and Standardization",
                context=rule_context,
            )
        except Exception as e:
            logger.warning(f"Failed to get dynamic cleaning prompt: {e}")
            return None

    async def clean_attributes(self, attributes: List[AttributeInput], context: ProductContext) -> LLMCleaningResponse:
        from app.aggregation.aggregate_product import call_llm_with_schema
        logger.info(f"Starting cleaning for {len(attributes)} attributes")
        if not attributes:
            return LLMCleaningResponse(
                cleaned_attributes=[],
                summary="No attributes to clean"
            )
        async with self._semaphore:

            prompt = await self._get_dynamic_prompt(attributes, context)
            if not prompt:
                logger.warning(
                    "No cleaning prompt configured in business rules")
                return self._fallback_response(
                    attributes,
                    "No cleaning prompt configured in business rules"
                )
            for attempt in range(self.max_retries):
                try:
                    estimated_tokens = 500 + len(attributes) * 200
                    await openai_limiter.wait_if_needed(estimated_tokens=estimated_tokens)
                    response = await call_llm_with_schema(
                        prompt=prompt,
                        response_model="LLMCleaningResponse",
                        llm_provider=self.llm_provider,
                        model=self.model,
                        estimated_tokens=estimated_tokens,
                        max_tokens=min(4000 + len(attributes) * 100, 8000)
                    )
                    logger.info(
                        f"LLM response received, cleaned {len(response.cleaned_attributes)} attributes")
                    input_ids = {a.id for a in attributes}
                    response_ids = {
                        ca.id for ca in response.cleaned_attributes}
                    missing_ids = input_ids - response_ids
                    if missing_ids:
                        logger.warning(
                            f"LLM missing attributes: {missing_ids}. Adding unchanged.")
                        for attr in attributes:
                            if attr.id in missing_ids:
                                response.cleaned_attributes.append(CleanedAttribute(
                                    id=attr.id,
                                    name=attr.name,
                                    original_value=attr.value,
                                    cleaned_value=attr.value,
                                    unit=attr.unit,
                                    cleaning_reason="No change (LLM omitted)",
                                    issue_detected=False
                                ))
                    return response
                except Exception as e:
                    logger.error(f"Cleaning attempt {attempt+1} failed: {e}")
                    if attempt == self.max_retries - 1:
                        return self._fallback_response(attributes, f"LLM error after {self.max_retries} attempts: {e}")
                    await asyncio.sleep(2 ** attempt)
        return self._fallback_response(attributes, "Max retries exceeded")

    async def get_global_name_mapping(
        self,
        attribute_names: List[str],
        project_id: str
    ) -> Optional[Dict[str, str]]:
        if not self.rule_engine or not attribute_names:
            return None

        try:

            context = {
                "attribute_names": attribute_names,
                "name_list": "\n".join([f"- {name}" for name in sorted(attribute_names)]),
                "total_count": len(attribute_names)
            }

            prompt = await self.rule_engine.get_active_prompt(
                stage="attribute_mapping",
                operation_mode="cleaning",
                use_case="Data cleaning and Standardization",
                context=context,
            )

            if not prompt:
                logger.warning(
                    "No attribute mapping prompt configured, using fallback")
                prompt = self._build_mapping_prompt(attribute_names)

            from app.aggregation.aggregate_product import call_llm_with_schema

            response = await call_llm_with_schema(
                prompt=prompt,
                response_model="AttributeMappingResponse",
                llm_provider=self.llm_provider,
                model=self.model,
                estimated_tokens=1000 + len(attribute_names) * 50,
                max_tokens=4000
            )

            if response and hasattr(response, 'mapping'):
                if isinstance(response.mapping, dict):
                    return response.mapping

                elif isinstance(response.mapping, list):
                    mapping_dict = {}
                    for p in response.mapping:
                        if hasattr(p, 'variant'):
                            mapping_dict[p.variant] = p.canonical
                        elif isinstance(p, dict):
                            mapping_dict[p.get('variant')] = p.get('canonical')
                    return mapping_dict

            if isinstance(response, dict):
                raw = response.get('mapping', {})
                if isinstance(raw, dict):
                    return raw
                if isinstance(raw, list):
                    return {p.get('variant', ''): p.get('canonical', '') for p in raw if isinstance(p, dict)}

            return None

        except Exception as e:
            logger.warning(f"Failed to get global name mapping: {e}")
            return None

    def _build_mapping_prompt(self, attribute_names: List[str]) -> str:
        name_list = "\n".join(
            [f"- {name}" for name in sorted(attribute_names)])

        return f"""
You are a product data standardization expert. Standardize these attribute names to canonical forms.

ATTRIBUTE NAMES FOUND IN CATALOG:
{name_list}

RULES:
1. Map all variants to a single canonical name in Title Case.
2. Common mappings:
   - "Color", "COLOUR", "Colour", "clr code", "COLOR CODE", "COLOUR CODE" → "Color"
   - "Voltage", "VOLTAGE", "Voltage Rating", "Voltage AC", "Voltage DC" → "Voltage"
   - "Material", "MATERIAL", "Materials" → "Material"
   - "Finish", "FINISH" → "Finish"
   - "Weight", "WEIGHT", "Approx. Wt." → "Weight"
   - "Length", "LENGTH", "Len" → "Length"
   - "Width", "WIDTH" → "Width"
   - "Height", "HEIGHT" → "Height"
   - "Amperage", "AMPERAGE", "Amps", "Current" → "Amperage"
   - "Product Type", "PRODUCT TYPE", "Type" → "Product Type"
   - "Grade", "GRADE", "Gade" → "Grade"
   - "Temperature Rating", "TEMPERATURE RATING", "Temp Rating" → "Temperature Rating"
   - "Bend Radius", "Bend Radius." → "Bend Radius"
   - "Wire Guide Ref", "Wire Guide Ref." → "Wire Guide Ref"
   - "Stripfeed", "Stripfeed?", "Stripfeed?(Y/N)" → "Stripfeed"

3. For acronyms that should stay uppercase: "MPN", "SKU", "UPC", "GTIN", "UL", "NEC", "ROHS", "ANSI", "AWG", "PSI", "VDC", "VAC"

Return a JSON object with mapping from original to canonical:
{{
  "mapping": [
    {{"variant": "COLOUR", "canonical": "Color"}},
    {{"variant": "VOLTAGE", "canonical": "Voltage"}}
  ]
}}

IMPORTANT: Include EVERY attribute name from the input list in the mapping.
"""

    
    

    def _fallback_response(self, attributes: List[AttributeInput], reason: str) -> LLMCleaningResponse:
        cleaned = []
        for attr in attributes:
            cleaned.append(CleanedAttribute(
                id=attr.id,
                name=attr.name,
                original_value=attr.value,
                cleaned_value=attr.value,
                unit=attr.unit,
                cleaning_reason=f"Fallback: {reason}",
                issue_detected=False
            ))
        return LLMCleaningResponse(
            cleaned_attributes=cleaned,
            summary=f"LLM unavailable, used original values. Reason: {reason}"
        )
