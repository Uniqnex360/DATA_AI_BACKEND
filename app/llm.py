from openai import OpenAI
import google.generativeai as genai
import json
import time
from app.core.config import settings
client = OpenAI(api_key=settings.openai_api_key,timeout=60.0    )
genai.configure(api_key=settings.gemini_api_key)

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