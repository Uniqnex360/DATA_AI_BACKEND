from openai import OpenAI
from .config import settings
import google.generativeai as genai
import json
from app.sacred import generate_search_queries

client = OpenAI(api_key=settings.openai_api_key)
genai.configure(api_key=settings.gemini_api_key)
def parse_response(content:str)->dict:
    content=content.strip()
    print(f"Raw content: {repr(content)}")
    if content.startswith("```json"):
        content = content[7:-3]
    elif content.startswith("```"):
        content = content[3:-3]
    print(f"After stripping: {repr(content)}")
    
def call_llm(prompt: str, schema: dict) -> dict:
    try:
        print(f"Using model: {settings.llm_model}")
        print(f"API key exists: {bool(settings.openai_api_key)}")
    
        response = client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "user", "content": prompt}
        ],
        response_format={"type": "json_object"},
        max_completion_tokens=4000
    )
        print(f"Full response: {response}")
        content = response.choices[0].message.content.strip()
        return parse_response(content)
    except Exception as e:
        print(f"Open AI failed:{str(e)}")
        print(f"---Switching  to Gemini backup ({settings.gemini_model})")
        try:
            model=genai.GenerativeModel(model_name=settings.gemini_model,generation_config={'response_mime_typ':'application/json'})
            gemini_prompt=f'{prompt}\n\Return JSON response matching this schema:{json.dumps(schema)}'
            response=model.generate_content(gemini_prompt)
            return parse_response(response.text)
        except Exception as e:
            print(f"Gemini Backup also failed: {str(e)}")
            return e