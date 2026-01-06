from openai import OpenAI
from .config import settings
import json

client = OpenAI(api_key=settings.openai_api_key)

def call_llm(prompt: str, schema: dict) -> dict:
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
    print(f"Choices: {response.choices}")
    print(f"First choice: {response.choices[0]}")
    print(f"Message: {response.choices[0].message}")
    print(f"Content: {response.choices[0].message.content}")
    
    content = response.choices[0].message.content.strip()
    
    print(f"Raw content: {repr(content)}")
    
    if content.startswith("```json"):
        content = content[7:-3]
    elif content.startswith("```"):
        content = content[3:-3]
    
    print(f"After stripping: {repr(content)}")
    
    return json.loads(content)