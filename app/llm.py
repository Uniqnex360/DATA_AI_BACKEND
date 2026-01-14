from openai import OpenAI
import google.generativeai as genai
import json
import time
from app.core.config import settings
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
    return json.loads(content)
    
def call_llm(prompt: str, schema: dict) -> dict:
    time.sleep(4)
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
            model=genai.GenerativeModel(model_name=settings.gemini_model,generation_config={'response_mime_type':'application/json'})
            gemini_prompt = f'{prompt}\n\nReturn JSON response matching this schema:{json.dumps(schema)}'
            response=model.generate_content(gemini_prompt)
            return parse_response(response.text)
        except Exception as e:
            print(f"Gemini Backup also failed: {str(e)}")
            return {"error": str(e)} 