from pydantic_settings import BaseSettings 
class Settings(BaseSettings):
    openai_api_key:str
    llm_model:str='gpt-5'
    gemini_api_key:str
    gemini_model: str = "gemini-1.5-flash"
    enrichment_confidence_threashold:float=0.8
    hitl_confidence_threashold:float=0.85
    cloudinary_cloud_name:str 
    cloudinary_api_key:str 
    cloudinary_api_secret:str 
    cloudinary_folder:str=''
    serpapi_key:str
    class Config:
        env_file='.env'
        env_file_encoding='utf-8'
settings=Settings()