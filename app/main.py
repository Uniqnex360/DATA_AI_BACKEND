from __future__ import annotations
from app.core.config import settings
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging
from pathlib import Path
from app.core.database import init_db
from app.core.smart_migration import smart_init_db
from app.api.v1.endpoints import auth,audit,users,golden_records,dashboard,products,rules,projects,extraction,cleansing,aggregation,standardization,enrichment,hitl,publishing,business_rules
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api_main")
app = FastAPI(title="Product Data Aggregation Engine - Production Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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

@app.on_event("startup")
async def on_startup():
    await smart_init_db()
@app.get('/health')
def health():
    return {'status': 'healthy'}
