import logging
import asyncio
from anthropic import AsyncAnthropic
from app.aggregation.services.smart_search import LinkJudgeResponse, ManufacturerScoringResponse, ManufacturerUrlResponse, ManufacturerWebsiteResponse, NavigationResponse, PageMatchScore, SimpleText, SmartSearchResponse, TargetedQueryResponse, UrlFilterResponse,ProductPageResponse
from app.core.rate_limiter import openai_limiter
from openai import OpenAI
import google.generativeai as genai
import json
import google.api_core.exceptions
from typing import Any, Optional
from openai import AsyncOpenAI
import random
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from app.core.config import settings
from app.aggregation.response_schemas import AggregationResponse, CleaningResponse, EnrichmentResponse, ExtractionResponse, StandardizationResponse, UnificationResponse, ValidationResponse
from app.aggregation.services.cleaning_service import LLMCleaningResponse
from app.schemas.aggregation import UnifiedStandardizedResponse
from app.schemas.pdf_extraction import AttributeMappingResponse, PDFExtractionResponse, ProductIdentificationResponse, SingleProductExtraction
client = OpenAI(api_key=settings.openai_api_key, timeout=60.0)
genai.configure(api_key=settings.gemini_api_key)
logger = logging.getLogger('llm')
_openai_client = AsyncOpenAI(
    api_key=settings.openai_api_key,
    timeout=90.0
)
_anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key) if settings.anthropic_api_key else None
_llm_semaphore = asyncio.Semaphore(5)
_gemini_lock = asyncio.Lock() 


def parse_response(content: str) -> dict:
    content = content.strip()
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0].strip()
    elif "```" in content:
        content = content.split("```")[1].split("```")[0].strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        print(f"JSON Parsing Error: {e} | Content: {content}")
        return {"error": "invalid_json", "raw": content}


def call_llm(prompt: str, schema: dict) -> dict:
    try:
        if "json" not in prompt.lower():
            prompt += "\n\nCRITICAL: Return the result in valid JSON format."
        full_prompt = f"{prompt}\n\nREQUIRED SCHEMA:\n{json.dumps(schema, indent=2)}"
        response = _openai_client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "user", "content": full_prompt}
            ],
            response_format={"type": "json_object"},
            max_completion_tokens=8000,
            timeout=60.0
        )
        content = response.choices[0].message.content.strip()
        return parse_response(content)
    except Exception as e:
        error_msg = str(e)
        print(f"Open AI failed: {error_msg[:200]}")
        print(f"---Switching  to Gemini backup ({settings.gemini_model})")
        try:
            model = genai.GenerativeModel(model_name=settings.gemini_model, generation_config={
                                          'response_mime_type': 'application/json'})
            gemini_prompt = f'{prompt}\n\nReturn JSON response matching this schema:{json.dumps(schema)}'
            response = model.generate_content(gemini_prompt)
            return parse_response(response.text)
        except Exception as e:
            print(f"Gemini Backup also failed: {str(e)}")
            return {"error": str(e)}


SCHEMA_MAP = {
    "ExtractionResponse": ExtractionResponse,
    "CleaningResponse": CleaningResponse,
    "UnificationResponse": UnificationResponse,
    "ValidationResponse": ValidationResponse,
    "AggregationResponse": AggregationResponse,
    "EnrichmentResponse": EnrichmentResponse,
    "UrlFilterResponse": UrlFilterResponse,
    "SmartSearchResponse": SmartSearchResponse,
    "LLMCleaningResponse": LLMCleaningResponse,
    "StandardizationResponse": StandardizationResponse,
    "UnifiedStandardizedResponse": UnifiedStandardizedResponse,
    "TargetedQueryResponse": TargetedQueryResponse,
    "PDFExtractionResponse":PDFExtractionResponse,
    "SingleProductExtraction":SingleProductExtraction,
    "ProductIdentificationResponse":ProductIdentificationResponse,
    "AttributeMappingResponse":AttributeMappingResponse,
    "ManufacturerWebsiteResponse": ManufacturerWebsiteResponse,
    "ProductPageResponse":ProductPageResponse,
    "ManufacturerScoringResponse":ManufacturerScoringResponse,
    "PageMatchScore":PageMatchScore,
    "SimpleText":SimpleText,
    "NavigationResponse": NavigationResponse,
    "ProductPageResponse": ProductPageResponse,
    "LinkJudgeResponse":LinkJudgeResponse,
    "ManufacturerUrlResponse":ManufacturerUrlResponse

}
@retry(
    retry=retry_if_exception_type(google.api_core.exceptions.ResourceExhausted),
    wait=wait_exponential(multiplier=2, min=10, max=60),
    stop=stop_after_attempt(3)
)
def _sync_call_gemini(model, prompt):
    return model.generate_content(prompt).text

async def call_gemini_safely(model_name, prompt):
    async with _gemini_lock:
        model = genai.GenerativeModel(model_name=model_name, generation_config={'response_mime_type': 'application/json'})
        response_text = await asyncio.to_thread(_sync_call_gemini, model, prompt)
        await asyncio.sleep(2) 
        return response_text

async def call_llm_with_schema(
    prompt: str,
    response_model: str,
    llm_provider:str,
    model: str = "gpt-4o-mini",
    estimated_tokens: int = 2000,
    max_tokens: Optional[int] = None
) -> Any:
    logger.info(f"CALL_LLM_WITH_SCHEMA - Using LLM provider: {llm_provider}")  
    if llm_provider and llm_provider not in ('openai','gemini','claude'):
        error_msg = (
            f"Invalid LLM provider: '{llm_provider}'. "
            f"Combined values like 'openai_gemini' must be split into primary and missing "
            f"BEFORE calling this function. Check the frontend and router code."
        )
        logger.error(error_msg)
        raise ValueError(error_msg)
    schema_class = SCHEMA_MAP.get(response_model)
    if not schema_class:
        raise ValueError(f"Unknown response model: {response_model}")
    try:
        if llm_provider=='openai':
            last_error = None
            for attempt in range(5):
                if attempt > 0 and last_error:
                    wait_time = (2 ** (attempt - 1)) + random.uniform(0, 1)
                    logger.warning(
                        f"Rate limit hit, waiting {wait_time:.1f}s (attempt {attempt + 1}/5)")
                    await asyncio.sleep(wait_time)
                async with _llm_semaphore:
                    try:
                        await openai_limiter.wait_if_needed(estimated_tokens=estimated_tokens)
                        response = await _openai_client.beta.chat.completions.parse(
                            model=model,
                            messages=[
                                {"role": "system", "content": "You are a precise data extraction engine. Follow instructions exactly. Never invent product information"},
                                {"role": "user", "content": prompt}
                            ],
                            response_format=schema_class,
                            max_tokens=max_tokens,
                            temperature=0
                        )
                        return response.choices[0].message.parsed
                    except Exception as e:
                        error_str = str(e)
                        if "429" in error_str or "rate_limit" in error_str.lower():
                            last_error = e
                            continue
                        else:
                            raise
            raise last_error
        elif llm_provider=='gemini':
            schema_dict=schema_class.model_json_schema()
            gemini_prompt=f"""{prompt}
            Return JSON response matching this schema:{json.dumps(schema_dict,indent=2)}
            """
            
            
            
            response_text = await call_gemini_safely(settings.gemini_model, gemini_prompt)
            parsed=parse_response(response_text)
            return schema_class.model_validate(parsed)
        elif llm_provider == 'claude':
            if not _anthropic_client:
                raise ValueError("Anthropic API key not configured")

            schema_dict = schema_class.model_json_schema()
            claude_prompt = f"""{prompt}
            Return JSON response matching this schema:{json.dumps(schema_dict, indent=2)}
            Do not include markdown fences or explanation text.
            """

            async with _llm_semaphore:
                response = await _anthropic_client.messages.create(
                    model=settings.claude_model,
                    max_tokens=max_tokens or 4000,
                    system="You are a precise data extraction engine. Follow instructions exactly. Never invent product information. Return only valid JSON.",
                    messages=[
                        {"role": "user", "content": claude_prompt}
                    ]
                )

            response_text = "".join(
                block.text for block in response.content
                if getattr(block, "type", None) == "text"
            )
            parsed = parse_response(response_text)
            return schema_class.model_validate(parsed)
    except Exception as e:
        logger.warning(f"Primary provider {llm_provider} failed. Switching to Gemini fallback...")
        try:
            
            schema_dict = schema_class.model_json_schema()
            fallback_prompt = f"{prompt}\n\nReturn JSON response matching this schema: {json.dumps(schema_dict)}"
            
            response_text = await call_gemini_safely(settings.gemini_model, fallback_prompt)
            parsed = parse_response(response_text)
            return schema_class.model_validate(parsed)

        except Exception as gemini_err:
            logger.error(f"Gemini fallback failed: {gemini_err}. Attempting OpenAI as a final last resort...")
            
            
            try:
                async with _llm_semaphore:
                    
                    response = await _openai_client.beta.chat.completions.parse(
                        model="gpt-4o-mini", 
                        messages=[
                            {"role": "system", "content": "You are a precise data extraction engine. Final attempt."},
                            {"role": "user", "content": prompt}
                        ],
                        response_format=schema_class,
                        timeout=120.0,
                        temperature=0
                    )
                    return response.choices[0].message.parsed
            except Exception as final_err:
                logger.critical(f"All providers (OpenAI/Gemini) failed: {final_err}")
                raise
