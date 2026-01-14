from pydantic_settings import BaseSettings 
from typing import Optional,List
class Settings(BaseSettings):
    API_V1_STR: str = "/api/v1"
    PROJECT_NAME:str='Datasphere PIM'
    BACKEND_CORS_ORIGINS: List[str] = ["http://localhost:5173", "http://localhost:3000"]
    SECRET_KEY:str
    ALGORITHM:str="HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 8
    DATABASE_URL: str = "postgresql+asyncpg://user:password@localhost:5432/datasphere_db"
    DB_POOL_SIZE:int=20
    DB_MAX_OVERFLOW:int=10
    DB_ECHO_LOG: bool = False
    openai_api_key:str
    llm_model:str='gpt-5'
    gemini_api_key:str
    gemini_model: str = "gemini-2.0-flash"
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
        extra = "ignore"
settings=Settings()