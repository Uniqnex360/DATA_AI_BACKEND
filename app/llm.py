from openai import OpenAI
import google.generativeai as genai
import json
import time
from typing import Any,Optional
from openai import AsyncOpenAI
from app.core.config import settings
from app.aggregation.response_schemas import AggregationResponse, CleaningResponse, EnrichmentResponse, ExtractionResponse, UnificationResponse, ValidationResponse
client = OpenAI(api_key=settings.openai_api_key,timeout=60.0    )
genai.configure(api_key=settings.gemini_api_key)
from app.core.rate_limiter import openai_limiter
import asyncio
import logging
logger=logging.getLogger('llm')
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
    # time.sleep(4)
    try:
        if "json" not in prompt.lower():
            prompt += "\n\nCRITICAL: Return the result in valid JSON format."
        full_prompt = f"{prompt}\n\nREQUIRED SCHEMA:\n{json.dumps(schema, indent=2)}"
    
        response = client.chat.completions.create(
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
            model=genai.GenerativeModel(model_name=settings.gemini_model,generation_config={'response_mime_type':'application/json'})
            gemini_prompt = f'{prompt}\n\nReturn JSON response matching this schema:{json.dumps(schema)}'
            response=model.generate_content(gemini_prompt)
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
    "EnrichmentResponse": EnrichmentResponse
}
async def call_llm_with_schema(
    prompt: str,
    response_model: str,
    model: str = "gpt-4o-2024-08-06",
    estimated_tokens: int = 2000,
    max_tokens: Optional[int] = None
) -> Any:
    """
    Call LLM with structured output schema
    
    Uses OpenAI's Structured Outputs feature for reliability
    """
    await openai_limiter.wait_if_needed(estimated_tokens=estimated_tokens)
    client = AsyncOpenAI()
    
    schema_class = SCHEMA_MAP.get(response_model)
    if not schema_class:
        raise ValueError(f"Unknown response model: {response_model}")
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = await client.beta.chat.completions.parse(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a precise data extraction engine. Follow instructions exactly.Never invent product information"},
                    {"role": "user", "content": prompt}
                ],
                response_format=schema_class,
                max_tokens=max_tokens
            )
            
            return response.choices[0].message.parsed
            
        except Exception as e:
            error_str = str(e)
            if "429" in error_str or "rate_limit" in error_str.lower():
                if attempt < max_retries - 1:
                    # Extract wait time if provided
                    wait_time = 2 ** attempt  # Exponential backoff
                    logger.warning(f"Rate limit hit, retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    logger.error(f"Rate limit persisted after {max_retries} retries")
                    raise
            else:
                # Not a rate limit error, re-raise
                logger.error(f"LLM call failed: {e}")
                raise