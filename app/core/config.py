from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    API_V1_STR: str = "/api/v1"
    PROJECT_NAME: str = 'Data AI Backend'
    BACKEND_CORS_ORIGINS: List[str] = ["http://localhost:5173",
                                       "http://localhost:3000", "https://data-ai-frontend-dusky.vercel.app"]
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 8
    DATABASE_URL: str
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 10
    DB_ECHO_LOG: bool = False
    openai_api_key: str
    ALGORITHM: str
    llm_model: str = 'gpt-4o-mini'
    MIGRATION_MODE: str = "safe"
    enrichment_threshold: int
    gemini_api_key: str
    anthropic_api_key: str
    PDF_UPLOAD_DIR: str = "/app/uploads/pdf"
    claude_model: str = "claude-sonnet-4-5"
    gemini_model: str = "gemini-2.5-flash-lite"
    enrichment_confidence_threashold: float = 0.8
    HITL_CONFIDENCE_THRESHOLD: float = 0.85
    cloudinary_cloud_name: str
    cloudinary_api_key: str
    cloudinary_api_secret: str
    SECRET_KEY: str
    FIRECRAWL_API_KEY: str
    SEARXNG_URL: str

    cloudinary_folder: str = ''
    serpapi_key: str

    class Config:
        env_file = '.env'
        env_file_encoding = 'utf-8'
        extra = "ignore"


settings = Settings()
