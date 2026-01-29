from __future__ import annotations

from app.core.config import settings
# from __future__ import annotations
# from fastapi import FastAPI, HTTPException
# from typing import Dict,List
# from .schemas import RawValue, StandardizedAttribute, EnrichmentResult
# from .cleaning import clean_attribute
# from .standardization import standardize_attribute
# from .enrichment import enrich_product
# from .hitl import check_for_human_review
# from .hitl_store import HITL_QUEUE
# from .config import settings
# from app.llm import call_llm
# from concurrent.futures import ThreadPoolExecutor, as_completed
# from .sacred import generate_search_queries,extract_from_web,extract_from_pdf,extract_from_image,standardize_with_llm
# from app.safe_aggregation import aggregate_product_safe
# app = FastAPI(title="Product Data Aggregation Engine - Production Backend")
# import pandas as pd
# import io
# from pathlib import Path
# import uuid
# from fastapi import File, UploadFile,BackgroundTasks
# import logging
# logger = logging.getLogger("api _main")
# logging.basicConfig(level=logging.INFO)
# BATCH_RESULTS = {}

# @app.get('/health')
# def health():
#     return {'status': 'healthy'}

# # def process_batch_in_background(batch_id: str, df_dict: List[Dict]):
# #     logger.info(f"Starting background processing for batch {batch_id}")
# #     final_output = []

# #     for i, row in enumerate(df_dict):
# #         row_clean = {str(k).strip().lower(): v for k, v in row.items()}
# #         mpn = row_clean.get("sku") or row_clean.get("mpn") or row_clean.get("part number")
# #         title = row_clean.get("product title") or row_clean.get("title")

# #         if not mpn and not title: continue

# #         result = aggregate_product_safe(
# #             mpn=str(mpn) if pd.notna(mpn) else None,
# #             title=str(title) if pd.notna(title) else None
# #         )

# #         # --- NEW: Extract and Format Source List ---
# #         # Collect all source URLs used in this aggregation
# #         source_links = result.get("golden_record", {}).get("sources", [])
# #         # If 'sources' is a list of strings, join them with a newline or comma
# #         sources_string = "\n".join(source_links) if source_links else "No sources found"

# #         # --- Flatten data for Excel ---
# #         attributes = result.get("golden_record", {}).get("attributes", {})

# #         excel_row = {
# #             "Input SKU": mpn,
# #             "Input Title": title,
# #             "Ready for Publish": result.get("ready_for_publish"),
# #             "Confidence": result.get("golden_record", {}).get("confidence"),
# #             "Sources Count": result.get("sources_used"),
# #             "Source URLs": sources_string,  # <--- NEW COLUMN ADDED HERE
# #             **attributes
# #         }
# #         final_output.append(excel_row)

# #         # Update progress
# #         BATCH_RESULTS[batch_id] = {"status": "processing", "progress": f"{i+1}/{len(df_dict)}"}

# #     # Save to Excel
# #     df_results = pd.DataFrame(final_output)
# #     file_path = f"./storage/batch_results_{batch_id}.xlsx"
# #     df_results.to_excel(file_path, index=False)

# #     BATCH_RESULTS[batch_id] = {
# #         "status": "completed",
# #         "progress": "100%",
# #         "excel_file": file_path,
# #         "results": final_output
# #     }

# def process_batch_in_background(batch_id: str, df_dict: List[Dict]):
#     logger.info(f"Starting PARALLEL background processing for batch {batch_id}")
#     final_output = []
#     total_items = len(df_dict)

#     with ThreadPoolExecutor(max_workers=10) as executor:
#         future_to_row = {}

#         for row in df_dict:
#             row_clean = {str(k).strip().lower(): v for k, v in row.items()}
#             mpn = row_clean.get("sku") or row_clean.get("mpn") or row_clean.get("part number")
#             title = row_clean.get("product title") or row_clean.get("title")

#             if not mpn and not title:
#                 continue

#             future = executor.submit(
#                 aggregate_product_safe,
#                 mpn=str(mpn) if pd.notna(mpn) else None,
#                 title=str(title) if pd.notna(title) else None
#             )
#             future_to_row[future] = (mpn, title)

#         completed_count = 0
#         for future in as_completed(future_to_row):
#             mpn, title = future_to_row[future]
#             try:
#                 result = future.result()

#                 source_links = result.get("golden_record", {}).get("sources", [])
#                 sources_string = "\n".join(source_links) if source_links else "No sources found"
#                 attributes = result.get("golden_record", {}).get("attributes", {})

#                 excel_row = {
#                     "Input SKU": mpn,
#                     "Input Title": title,
#                     "Ready for Publish": result.get("ready_for_publish"),
#                     "Confidence": result.get("golden_record", {}).get("confidence"),
#                     "Sources Count": result.get("sources_used"),
#                     "Source URLs": sources_string,
#                     **attributes
#                 }
#                 final_output.append(excel_row)
#             except Exception as e:
#                 logger.error(f"Row failed for {mpn}: {e}")

#             completed_count += 1
#             BATCH_RESULTS[batch_id] = {
#                 "status": "processing",
#                 "progress": f"{completed_count}/{total_items}"
#             }

#     df_results = pd.DataFrame(final_output)
#     file_path = f"./storage/batch_results_{batch_id}.xlsx"
#     df_results.to_excel(file_path, index=False)

#     BATCH_RESULTS[batch_id] = {
#         "status": "completed",
#         "progress": "100%",
#         "excel_file": file_path,
#         "results": final_output
#     }
# @app.post('/clean')
# def clean(payload: Dict):
#     result = {}
#     for attr, attr_data in payload.items():
#         raw_values = [RawValue(**item) for item in attr_data["values"]]
#         cleaned = clean_attribute(raw_values)
#         result[attr] = cleaned.dict()
#     return result


# @app.post('/standardize')
# def standardize(payload: Dict):
#     product_key = payload["product_key"]
#     data = payload["data"]

#     result = {}
#     review_required = False

#     for attr, attr_data in data.items():
#         raw_values = [RawValue(**item) for item in attr_data["values"]]
#         cleaned = clean_attribute(raw_values)

#         if not cleaned.valid_values:
#             continue

#         standardized = standardize_attribute(attr, cleaned.valid_values)

#         if check_for_human_review(product_key, attr, standardized):
#             review_required = True
#         else:
#             result[attr] = standardized.dict()

#     return {
#         "product_key": product_key,
#         "standardized": result,
#         "review_required": review_required,
#         "hitl_queue_size": len(HITL_QUEUE.get(product_key, []))
#     }


# @app.post('/enrich')
# def enrich(payload: Dict):
#     product_key = payload["product_key"]
#     brand = payload["brand"]
#     category = payload["category"]
#     standardized_attributes = payload["standardized_attributes"]

#     enrichment = enrich_product(brand, category, standardized_attributes)
#     review_required = enrichment.confidence < settings.enrichment_confidence_threashold

#     return {
#         "product_key": product_key,
#         "enrichment": enrichment.dict(),
#         "review_required": review_required
#     }

# @app.post("/unify-attributes")
# def unify_attributes(attributes: list[str]):
#     prompt = f"""
# You are a semantic attribute harmonization engine.
# Raw attribute names from multiple sources:
# {attributes}

# Task:
# - Identify which attributes mean the same thing
# - Group them under ONE canonical attribute in snake_case
# - Do NOT invent new attributes
# - Return only valid JSON

# Example output:
# {{
#   "canonical_attributes": {{
#     "screen_size": {{
#       "synonyms": ["Display Size", "Screen Size", "Diagonal", "Size"],
#       "confidence": 0.99
#     }},
#     "ip_rating": {{
#       "synonyms": ["Water Rating", "Waterproof Rating", "Ingress Protection"],
#       "confidence": 0.97
#     }}
#   }}
# }}
# """

#     schema = {
#         "name": "unification",
#         "schema": {
#             "type": "object",
#             "properties": {
#                 "canonical_attributes": {
#                     "type": "object",
#                     "additionalProperties": {
#                         "type": "object",
#                         "properties": {
#                             "synonyms": {"type": "array", "items": {"type": "string"}},
#                             "confidence": {"type": "number", "minimum": 0, "maximum": 1}
#                         },
#                         "required": ["synonyms", "confidence"]
#                     }
#                 }
#             },
#             "required": ["canonical_attributes"]
#         }
#     }

#     result = call_llm(prompt, schema)
#     return result
# @app.get('/hitl/pending')
# def get_pending_reviews():
#     return HITL_QUEUE


# @app.post("/hitl/approve")
# def approve_item(product_key: str, attribute: str, reviewer: str):
#     items = HITL_QUEUE.get(product_key, [])
#     for item in items:
#         if item["attribute"] == attribute:
#             item["status"] = "approved"
#             item["reviewer"] = reviewer
#             return item
#     raise HTTPException(404, "Item not found")


# @app.post('/hitl/override')
# def override_item(product_key: str, attribute: str, new_value: str, reviewer: str):
#     items = HITL_QUEUE.get(product_key, [])
#     for item in items:
#         if item["attribute"] == attribute:
#             item["overridden_value"] = new_value
#             item["status"] = "approved"
#             item["reviewer"] = reviewer
#             return item
#     raise HTTPException(404, "Item not found")

# @app.post("/aggregate")
# def aggregate(mpn: str = None, upc: str = None, title: str = None):
#     return aggregate_product_safe(mpn=mpn, upc=upc, title=title)


# @app.post('/hitl/reject')
# def reject_item(product_key: str, attribute: str, reviewer: str):
#     items = HITL_QUEUE.get(product_key, [])
#     if not items:
#         raise HTTPException(status_code=404,detail='Items not found')
#     for item in items:
#         if item.attribute == attribute:
#             item.status = "rejected"
#             item.reviewer = reviewer
#             return {"status": "rejected", "item": item}
#     raise HTTPException(status_code=404, detail="Item not found in HITL queue")

# @app.post("/batch-aggregate")
# async def batch_aggregate(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
#     content = await file.read()
#     df = pd.read_excel(io.BytesIO(content))

#     batch_id = str(uuid.uuid4())[:8]
#     df_dict = df.to_dict('records')

#     BATCH_RESULTS[batch_id] = {
#         "status": "queued",
#         "progress": f"0/{len(df_dict)}",
#         "results": []
#     }

#     background_tasks.add_task(process_batch_in_background, batch_id, df_dict)

#     return {
#         "message": "Batch processing started in background",
#         "batch_id": batch_id,
#         "total_items": len(df_dict),
#         "check_status_at": f"/batch-status/{batch_id}"
#     }
# @app.get("/batch-status/{batch_id}")
# def get_batch_status(batch_id: str):
#     batch = BATCH_RESULTS.get(batch_id)
#     if not batch:
#         raise HTTPException(404, "Batch ID not found")
#     return batch
from fastapi import FastAPI, HTTPException, File, UploadFile, BackgroundTasks
from typing import Dict, List
from app.schemas.enrichment import RawValue, StandardizedAttribute, EnrichmentResult
from .cleaning import clean_attribute
from .standardization import standardize_attribute
from .enrichment import enrich_product
from .hitl import check_for_human_review
from .hitl_store import HITL_QUEUE
from app.llm import call_llm
from fastapi.middleware.cors import CORSMiddleware
from concurrent.futures import ThreadPoolExecutor, as_completed
from .sacred import generate_search_queries, extract_from_web, extract_from_pdf, extract_from_image, standardize_with_llm
from app.safe_aggregation import aggregate_product_safe
import time
import pandas as pd
import io
import json
import os
import uuid
import logging
from pathlib import Path
from app.core.database import init_db
from app.api.v1.endpoints import auth,audit,users,golden_records,dashboard,products,rules,projects,extraction,cleansing,aggregation,standardization,enrichment,hitl,publishing
# Setup Logging
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
app.include_router(extraction.router, prefix=f"{settings.API_V1_STR}/extract", tags=["extract"])
app.include_router(aggregation.router, prefix=f"{settings.API_V1_STR}/aggregation", tags=["aggregation"])
app.include_router(standardization.router, prefix=f"{settings.API_V1_STR}/standardization", tags=["standardization"])
app.include_router(enrichment.router, prefix=f"{settings.API_V1_STR}/enrichment", tags=["enrichment"])
app.include_router(hitl.router, prefix=f"{settings.API_V1_STR}/hitl", tags=["hitl"])
# app/main.py

app.include_router(publishing.router, prefix=f"{settings.API_V1_STR}/publishing", tags=["publishing"])










STATUS_DIR = "./storage/status"
os.makedirs(STATUS_DIR, exist_ok=True)


@app.on_event("startup")
async def on_startup():
    await init_db()


def save_batch_status(batch_id, data):
    """Saves status to a JSON file so all workers can see it."""
    with open(f"{STATUS_DIR}/{batch_id}.json", "w") as f:
        json.dump(data, f)


def load_batch_status(batch_id):
    """Reads status from the JSON file."""
    path = f"{STATUS_DIR}/{batch_id}.json"
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except:
        return None

    
@app.get('/health')
def health():
    return {'status': 'healthy'}


def process_batch_in_background(batch_id: str, df_dict: List[Dict]):
    logger.info(
        f"Starting SEQUENTIAL background processing for batch {batch_id}")
    final_output = []
    total_items = len(df_dict)

    save_batch_status(batch_id, {
        "status": "processing",
        "progress": f"0/{total_items}",
        "results": []
    })

    completed_count = 0
    for row in df_dict:
        row_clean = {str(k).strip().lower(): v for k, v in row.items()}
        mpn = row_clean.get("sku") or row_clean.get(
            "mpn") or row_clean.get("part number")
        title = row_clean.get("product title") or row_clean.get("title")

        if not mpn and not title:
            continue

        try:
            result = aggregate_product_safe(
                mpn=str(mpn) if pd.notna(mpn) else None,
                title=str(title) if pd.notna(title) else None
            )

            source_links = result.get("golden_record", {}).get("sources", [])
            sources_string = "\n".join(
                source_links) if source_links else "No sources found"
            attributes = result.get("golden_record", {}).get("attributes", {})

            excel_row = {
                "Input SKU": mpn,
                "Input Title": title,
                "Ready for Publish": result.get("ready_for_publish"),
                "Confidence": result.get("golden_record", {}).get("confidence"),
                "Sources Count": result.get("sources_used"),
                "Source URLs": sources_string,
                **attributes
            }
            final_output.append(excel_row)

            time.sleep(10)

        except Exception as e:
            logger.error(f"Row failed for {mpn}: {e}")

        completed_count += 1

        save_batch_status(batch_id, {
            "status": "processing",
            "progress": f"{completed_count}/{total_items}"
        })

    df_results = pd.DataFrame(final_output)
    file_path = f"./storage/batch_results_{batch_id}.xlsx"
    os.makedirs("./storage", exist_ok=True)
    df_results.to_excel(file_path, index=False)

    save_batch_status(batch_id, {
        "status": "completed",
        "progress": "100%",
        "excel_file": file_path,
        "results": []
    })


@app.post("/batch-aggregate")
async def batch_aggregate(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    content = await file.read()
    df = pd.read_excel(io.BytesIO(content))

    batch_id = str(uuid.uuid4())[:8]
    df_dict = df.to_dict('records')

    save_batch_status(batch_id, {
        "status": "queued",
        "progress": f"0/{len(df_dict)}",
        "results": []
    })

    background_tasks.add_task(process_batch_in_background, batch_id, df_dict)

    return {
        "message": "Batch processing started in background",
        "batch_id": batch_id,
        "total_items": len(df_dict),
        "check_status_at": f"/batch-status/{batch_id}"
    }


@app.get("/batch-status/{batch_id}")
def get_batch_status(batch_id: str):
    batch = load_batch_status(batch_id)
    if not batch:
        raise HTTPException(404, "Batch ID not found")
    return batch


@app.post('/clean')
def clean(payload: Dict):
    result = {}
    for attr, attr_data in payload.items():
        raw_values = [RawValue(**item) for item in attr_data["values"]]
        cleaned = clean_attribute(raw_values)
        result[attr] = cleaned.dict()
    return result


@app.post('/standardize')
def standardize(payload: Dict):
    product_key = payload["product_key"]
    data = payload["data"]

    result = {}
    review_required = False

    for attr, attr_data in data.items():
        raw_values = [RawValue(**item) for item in attr_data["values"]]
        cleaned = clean_attribute(raw_values)

        if not cleaned.valid_values:
            continue

        standardized = standardize_attribute(attr, cleaned.valid_values)

        if check_for_human_review(product_key, attr, standardized):
            review_required = True
        else:
            result[attr] = standardized.dict()

    return {
        "product_key": product_key,
        "standardized": result,
        "review_required": review_required,
        "hitl_queue_size": len(HITL_QUEUE.get(product_key, []))
    }


@app.post('/enrich')
def enrich(payload: Dict):
    product_key = payload["product_key"]
    brand = payload["brand"]
    category = payload["category"]
    standardized_attributes = payload["standardized_attributes"]

    enrichment = enrich_product(brand, category, standardized_attributes)
    review_required = enrichment.confidence < settings.enrichment_confidence_threashold

    return {
        "product_key": product_key,
        "enrichment": enrichment.dict(),
        "review_required": review_required
    }



@app.get('/hitl/pending')
def get_pending_reviews():
    return HITL_QUEUE


@app.post("/hitl/approve")
def approve_item(product_key: str, attribute: str, reviewer: str):
    items = HITL_QUEUE.get(product_key, [])
    for item in items:
        if item["attribute"] == attribute:
            item["status"] = "approved"
            item["reviewer"] = reviewer
            return item
    raise HTTPException(404, "Item not found")


@app.post('/hitl/override')
def override_item(product_key: str, attribute: str, new_value: str, reviewer: str):
    items = HITL_QUEUE.get(product_key, [])
    for item in items:
        if item["attribute"] == attribute:
            item["overridden_value"] = new_value
            item["status"] = "approved"
            item["reviewer"] = reviewer
            return item
    raise HTTPException(404, "Item not found")


@app.post("/aggregate")
def aggregate(mpn: str = None, upc: str = None, title: str = None):
    return aggregate_product_safe(mpn=mpn, upc=upc, title=title)


@app.post('/hitl/reject')
def reject_item(product_key: str, attribute: str, reviewer: str):
    items = HITL_QUEUE.get(product_key, [])
    if not items:
        raise HTTPException(status_code=404, detail='Items not found')
    for item in items:
        if item.attribute == attribute:
            item.status = "rejected"
            item.reviewer = reviewer
            return {"status": "rejected", "item": item}
    raise HTTPException(status_code=404, detail="Item not found in HITL queue")
