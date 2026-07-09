from __future__ import annotations
import asyncio
from app.core.config import settings
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging
from pathlib import Path
import os
import warnings

from app.workers.canonical_worker import start_canonical_worker
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
from app.core.database import init_db
from sentence_transformers import SentenceTransformer
from app.api.v1.endpoints import auth,audit, pdf_extraction,users,golden_records,dashboard,products,rules,projects,extraction,cleansing,aggregation,standardization,enrichment,hitl,publishing,business_rules,reporting
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api_main")
logger.info("MASTER PROCESS: Pre-loading SentenceTransformer (Shared Memory)...")
try:
    _global_embedding_model = SentenceTransformer('all-MiniLM-L6-v2', device='cpu')
    logger.info("✓ Shared model weights loaded into RAM")
except Exception as e:
    logger.error(f"Failed to pre-load model: {e}")
    _global_embedding_model = None
app = FastAPI(title="Product Data Aggregation Engine - Production Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"], 
)
app.include_router(products.router, prefix=f"{settings.API_V1_STR}/products", tags=["products"])
app.include_router(auth.router, prefix=f"{settings.API_V1_STR}/auth", tags=["auth"])
app.include_router(audit.router, prefix=f"{settings.API_V1_STR}/audit-trail", tags=["audit"])
app.include_router(users.router, prefix=f"{settings.API_V1_STR}/users", tags=["users"])
app.include_router(golden_records.router, prefix=f"{settings.API_V1_STR}/golden-records", tags=["golden"])
app.include_router(dashboard.router, prefix=f"{settings.API_V1_STR}/dashboard", tags=["dashboard"])
app.include_router(rules.router, prefix=f"{settings.API_V1_STR}/rules", tags=["rules"])
app.include_router(projects.router, prefix=f"{settings.API_V1_STR}/projects", tags=["projects"])
app.include_router(extraction.router, prefix=f"{settings.API_V1_STR}/sources", tags=["sources"])
app.include_router(cleansing.router, prefix=f"{settings.API_V1_STR}/cleansing", tags=["cleansing"])
app.include_router(aggregation.router, prefix=f"{settings.API_V1_STR}/aggregation", tags=["aggregation"])
app.include_router(standardization.router, prefix=f"{settings.API_V1_STR}/standardization", tags=["standardization"])
app.include_router(enrichment.router, prefix=f"{settings.API_V1_STR}/enrichment", tags=["enrichment"])
app.include_router(hitl.router, prefix=f"{settings.API_V1_STR}/hitl", tags=["hitl"])
app.include_router(publishing.router, prefix=f"{settings.API_V1_STR}/publishing", tags=["publishing"])
app.include_router(business_rules.router, prefix=f"{settings.API_V1_STR}/business-rules", tags=["business_rules"])
app.include_router(pdf_extraction.router, prefix=f"{settings.API_V1_STR}/extraction/pdf", tags=["pdf_extraction"])
app.include_router(auth.router,prefix=f"{settings.API_V1_STR}/auth",tags=['auth'])
app.include_router(reporting.router,prefix=f"{settings.API_V1_STR}/reporting",tags=['reporting'])
warnings.filterwarnings(
    "ignore",
    category=UserWarning,
    message=".*Expected `float` but got `Decimal`.*"
)


@app.on_event("startup")
async def on_startup():
    await init_db()
    asyncio.create_task(start_canonical_worker(llm_provider="openai"))
@app.get('/health')
def health():
    return {'status': 'healthy'}
