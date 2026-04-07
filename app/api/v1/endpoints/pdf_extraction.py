from io import BytesIO
import pdfplumber
import json
from  app.core.config import settings
from sqlmodel import select
from sqlalchemy import  func, case
from sqlalchemy import func  
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import APIRouter, BackgroundTasks,Depends, File, Form, HTTPException, UploadFile
from datetime import datetime
import aiohttp
from openai import BaseModel, project, timeout
from app.aggregation.services.pdf_service import PDFExtractionService
from app.core.database import get_session
from app.llm import call_llm_with_schema
from app.models.pipeline import Source
from app.models.product import Product
from app.models.project import Project
from app.schemas.pdf_extraction import FreshAggregationRequest, PDFExtractionResponse
import uuid
import logging
from typing import List,Optional,Dict   
from app.search.searxng_service import SearXNGSearchService
from app.core.database import async_session_factory
logger=logging.getLogger('pdf extraction')
router = APIRouter()
async def sync_project_status(db, project_id: str) -> None:
    project = await db.get(Project, project_id)
    if not project:
        return

    stmt = select(
        func.count(Product.id),
        func.sum(case((Product.enrichment_status == "completed", 1), else_=0)),
        func.sum(case((Product.enrichment_status == "failed", 1), else_=0)),
        func.sum(case((Product.enrichment_status == "processing", 1), else_=0)),
        func.sum(case((Product.enrichment_status == "pending", 1), else_=0)),
    ).where(Product.project_id == project_id)

    result = await db.execute(stmt)
    row = result.one()

    total = row[0] or 0
    completed = row[1] or 0
    failed = row[2] or 0
    processing = row[3] or 0
    pending = row[4] or 0

    if total == 0:
        project.status = "draft"
    elif processing > 0:
        project.status = "processing"
    elif completed == total:
        project.status = "completed"
    elif failed == total:
        project.status = "failed"
    elif completed > 0:
        project.status = "partially_completed"
    else:
        project.status = "draft"

    db.add(project)
    await db.commit()
    
@router.post('/fresh-aggregation')
async def fresh_aggregation(request:FreshAggregationRequest,background_tasks:BackgroundTasks,db:AsyncSession=Depends(get_session)):
    try:
        batch_id = str(uuid.uuid4())
        source=Source(id=batch_id,source_type="pdf_fresh_aggregation",source_url='web_enrichment',project_id=request.project_id,status='processing',source_metadata={
            'mpns':request.mpns,
            'use_case':request.use_case,
            'started_at':datetime.utcnow().isoformat(),
            'total':len(request.mpns),
            'successful':0,
            'failed':0,
            'products':[],
            'processing_status': 'processing'
        })
        db.add(source)
        await db.commit()
        background_tasks.add_task(
            process_fresh_pdf_aggregation,batch_id,request.mpns,request.project_id
        )
        return {
            'status':'processing',
            'batch_id':batch_id,
            'message':f"Processing {len(request.mpns)}MPN(s)"
        }
    except Exception as e:
        logger.error(f"Failed to start fresh aggregation: {e}")
        raise HTTPException(500,str(e))
async def search_pdfs_url(mpn:str,brand:str)->List[str]:
    try:
        logger.info(f"🔍 [1/4] Starting PDF search for MPN: {mpn}")
        searxng = SearXNGSearchService(base_url="http://searxng:8080", max_results=20)
        search_queries=[
            f"{mpn} datasheet pdf",
            f"{mpn} specification sheet pdf",
            f"{mpn} data sheet pdf",
            f"{brand} {mpn} product pdf" if brand else f"{mpn} product pdf"
        ]
        logger.info(f"📋 Search queries: {search_queries}")
        pdf_urls=[]
        for query in search_queries:
            logger.info(f"🔎 Executing search: {query}")
            try:
                results=await searxng._search(query)
                logger.info(f"   Found {len(results)} results for query: {query}")
                for result in results:
                    url=result.get('url','')
                    if url and url.lower().endswith('.pdf'):
                        pdf_urls.append(url)
                        logger.info(f"   ✅ PDF found: {url}")
                    else:
                        logger.debug(f"   ⏭️ Skipped non-PDF: {url[:80]}...")
            except Exception as e:
                logger.debug(f"Search failed for {query}: {e}")
        return list(set(pdf_urls))[:5]
    except Exception as e:
        logger.error(f"Failed to search pdf urls: {e}")
        raise HTTPException(500,str(e))
async def download_and_extract_pdf(pdf_url:str)->Optional[str]:
    pdf_service=PDFExtractionService(max_pages=15)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(pdf_url,timeout=30) as resp:
                if resp.status==200:
                    pdf_bytes=await resp.read()
                    text=await pdf_service.extract_text(pdf_bytes)
                    if text and len(text.strip()) > 200:
                        logger.info(f'Extracted {len(text)} chars from {pdf_url}')
                        return text
    except Exception as e:
        logger.warning(f"Failed to download/extract PDF {pdf_url}: {e}")
    return None
async def extract_product_with_claude(mpn:str,pdf_text:str,source_url:str)->Optional[Dict]:
    try:
        prompt=f"""
        You are a product data extraction expert.Extract product information from the following PDF datasheet content.
        MPN:{mpn}
        PDF Content:
        {pdf_text[:12000]}
        Extract the following fields. If a field is not found, leave it empty:
        - product_name: Full product name
        - brand_name: Manufacturer/brand name
        - sku: Stock Keeping Unit (if available)
        - taxonomy: Product category (e.g., "Electronics > Computers > Laptops")
        - description: Product description/summary
        - image_url: Main product image URL (if referenced in the PDF)
        - specifications: Key technical specifications as key-value pairs (e.g., "Voltage": "12V", "Weight": "2.5kg")
        Return ONLY valid JSON
        """
        schema={
            "type":'object',
            "properties": {
            "product_name": {"type": "string"},
            "brand_name": {"type": "string"},
            "sku": {"type": "string"},
            "taxonomy": {"type": "string"},
            "description": {"type": "string"},
            "image_url": {"type": "string"},
            "specifications": {"type": "object"}
        }
        }
        extracted=await call_llm_with_schema(prompt=prompt,response_model="PDFExtractionResponse",llm_provider='claude',estimated_tokens=4000)
        if extracted:
            result = extracted.model_dump() if hasattr(extracted, 'model_dump') else extracted.dict()
            result['mpn']=mpn
            result['source_url']=source_url
            return result
    except Exception as e:
        logger.error(f"Claude extraction failed for {mpn}: {e}")
    return None
async def process_fresh_pdf_aggregation(batch_id: str, mpns: List[str], project_id: str):
    try:
        async with async_session_factory() as db:
            try:
                products = []
                successful = 0
                routed_to_enrichment_count = 0
                ready_for_export_count = 0
                enrichment_threshold = settings.enrichment_threshold
                failed_mpns = []
                for idx, mpn in enumerate(mpns):
                    try:
                        logger.info(f"Processing MPN {idx+1}/{len(mpns)}: {mpn}")
                        pdf_urls = await search_pdfs_url(mpn, brand='')
                        if not pdf_urls:
                            logger.warning(f"No PDF urls found for {mpn}")
                            failed_mpns.append(mpn)
                            continue
                        product_data = None
                        for pdf_url in pdf_urls:
                            pdf_text = await download_and_extract_pdf(pdf_url)
                            if pdf_text:
                                product_data = await extract_product_with_claude(mpn, pdf_text, pdf_url)
                                if product_data:
                                    break
                        if product_data:
                            specifications = product_data.get('specifications', {})
                            spec_count = len(specifications)
                            completeness_score = min(spec_count * 5, 100)
                            if completeness_score < enrichment_threshold:
                                workflow_stage = 'enrichment'
                                needs_enrichment = True
                                is_ready_for_export = False
                                routed_to_enrichment_at = datetime.utcnow()
                                enrichment_status = 'pending'
                                routed_to_enrichment_count += 1
                            else:
                                workflow_stage = 'aggregation'
                                needs_enrichment = False
                                is_ready_for_export = True
                                routed_to_enrichment_at = None
                                enrichment_status = 'completed'
                                ready_for_export_count += 1
                            product = Product(
                                product_code=mpn,
                                product_name=product_data.get('product_name', f"Product {mpn}"),
                                brand_name=product_data.get("brand_name", ""),
                                mpn=mpn,
                                sku=product_data.get("sku", ""),
                                taxonomy=product_data.get('taxonomy', ''),
                                description=product_data.get('description', ''),
                                image_url_1=product_data.get('image_url'),
                                project_id=project_id,
                                workflow_stage=workflow_stage,
                                enrichment_status=enrichment_status,
                                ready_for_export=is_ready_for_export,
                                needs_enrichment=needs_enrichment,
                                routed_to_enrichment_at=routed_to_enrichment_at,
                                attributes=specifications,
                                source_url=product_data.get('source_url', ""),
                                completeness_score=completeness_score
                            )
                            db.add(product)
                            products.append(product_data)
                            successful += 1
                            logger.info(f"Successfully extracted product for {mpn} (score {completeness_score})")
                        else:
                            failed_mpns.append(mpn)
                            logger.warning(f"Failed to extract product for {mpn}")
                    except Exception as e:
                        logger.error(f"Error processing MPN {mpn}: {e}")
                        failed_mpns.append(mpn)
                    if (idx + 1) % 3 == 0 or idx + 1 == len(mpns):
                        source = await db.get(Source, batch_id)
                        if source:
                            source.source_metadata.update({
                                'successful': successful,
                                'failed': len(failed_mpns),
                                'current_index': idx + 1,
                                'products': products,
                                'failed_mpns': failed_mpns,
                                'routed_to_enrichment': routed_to_enrichment_count,
                                'ready_for_export': ready_for_export_count
                            })
                            db.add(source)
                            await db.commit()
                source = await db.get(Source, batch_id)
                if source:
                    source.status = 'completed'
                    source.source_metadata.update({
                        'completed_at': datetime.utcnow().isoformat(),
                        'products': products,
                        'failed_mpns': failed_mpns,
                        'routed_to_enrichment': routed_to_enrichment_count,
                        'processing_status': 'completed',    
                        'ready_for_export': ready_for_export_count
                    })
                    db.add(source)
                    await db.commit()
                    await sync_project_status(db, project_id)
                logger.info(f"Fresh aggregation completed: {successful} successful, {len(failed_mpns)} failed, {routed_to_enrichment_count} to enrichment, {ready_for_export_count} ready")
            except Exception as e:
                logger.error(f"Fresh PDF aggregation failed: {e}")
                source = await db.get(Source, batch_id)
                if source:
                    source.status = 'failed'
                    source.source_metadata['error'] = str(e)
                    source.source_metadata['processing_status'] = 'failed'
                    db.add(source)
                    await db.commit()
                    await sync_project_status(db, project_id)
                    
    except Exception as e:
        logger.error(f"Fresh PDF aggregation failed: {e}")
@router.get('/batch-status/{batch_id}')
async def get_batch_status(batch_id:str,db:AsyncSession=Depends(get_session)):
    try:
        source=await db.get(Source,batch_id)
        if not source:
            raise HTTPException(404,"Batch not found")
        return {
            'status':source.status,
            'source_metadata':source.source_metadata,
            'created_at':source.created_at,
            'updated_at':source.updated_at
        }
    except Exception as e:
        raise e
@router.post('/structured-extraction')
async def structured_pdf_extraction(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    mpn: str = Form(...),
    project_id: str = Form(...),
    use_case: str = Form(...),
    db: AsyncSession = Depends(get_session),
):
    try:
        batch_id=str(uuid.uuid4())
        pdf_bytes=await file.read()
        source=Source(
            id=batch_id,
            source_type='pdf_structured_extraction',
            source_url=file.filename,
            project_id=project_id,
            status='processing',
            source_metadata={
                'mpn':mpn,
                'use_case':use_case,
                'started_at':datetime.utcnow().isoformat(),
                'processing_status': 'processing',
                'successful':0,
                'failed':0
            }
        )
        db.add(source)
        await db.commit()
        background_tasks.add_task(process_structured_pdf_extraction,batch_id,pdf_bytes,mpn,project_id,file.filename)
        return {
            'status':'processing',
            'batch_id':batch_id,
            'message':f'Processing MPN {mpn} from PDF'
        }
    except Exception as e:
         logger.error(f"Failed to start structured extraction: {e}")
         raise HTTPException(500,str(e))
async def process_structured_pdf_extraction(
    batch_id: str,
    pdf_bytes: bytes,
    mpn: str,
    project_id: str,
    filename: str
) -> None:
    """
    Background task: extract product data for a single MPN from an uploaded PDF.
    No external web search – only the PDF content is used.
    """
    async with async_session_factory() as db:
        try:
            full_text = ""
            tables = []
            with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        full_text += page_text + "\n"
                    page_tables = page.extract_tables()
                    for tbl in page_tables:
                        if tbl and len(tbl) > 1:          
                            headers = [str(h).strip() if h else "" for h in tbl[0]]
                            for row in tbl[1:]:
                                if not any(cell for cell in row):
                                    continue
                                row_dict = {}
                                for idx, cell in enumerate(row):
                                    if idx < len(headers):
                                        key = headers[idx]
                                        val = str(cell).strip() if cell else ""
                                        if key and val:
                                            row_dict[key] = val
                                if row_dict:
                                    tables.append(row_dict)
            truncated_text = full_text[:15000]
            truncated_tables = json.dumps(tables, indent=2)[:5000]
            prompt = f"""
You are a product data extraction expert. Extract product information for MPN "{mpn}" from the document below.
Use only the content provided; do not search externally.
Document content (text):
{truncated_text}
Table data (extracted from the document):
{truncated_tables}
Extract the following fields if present. If a field is missing, leave it empty:
- product_name
- brand_name
- sku
- taxonomy
- description
- image_url
- specifications
Return ONLY a single valid JSON object in this format:
{{
  "product_name": "",
  "brand_name": "",
  "sku": "",
  "taxonomy": "",
  "description": "",
  "image_url": "",
  "specifications": {{}}
}}
"""
            from pydantic import BaseModel, Field
            from typing import Dict
            logger.info(f"Extracted text length for {mpn}: {len(full_text)}")
            logger.info(f"Extracted text preview for {mpn}: {full_text[:1000]}")
            logger.info(f"Extracted tables count for {mpn}: {len(tables)}")
            response = await call_llm_with_schema(
                prompt=prompt,
                response_model="PDFExtractionResponse",
                llm_provider='claude',
                estimated_tokens=4000
            )
            product_info = None
            resp_dict = None
            if response:
                if hasattr(response, "model_dump"):
                    resp_dict = response.model_dump()
                elif hasattr(response, "dict"):
                    resp_dict = response.dict()
                elif isinstance(response, dict):
                    resp_dict = response
            logger.info(f"LLM parsed response for {mpn}: {resp_dict}")
            if resp_dict:
                if isinstance(resp_dict.get("data"), dict):
                    product_info = resp_dict["data"].get(mpn)
                elif mpn in resp_dict:
                    product_info = resp_dict[mpn]
                elif "product_name" in resp_dict or "specifications" in resp_dict:
                    product_info = resp_dict
            if not product_info:
                logger.warning(f"MPN {mpn} not found or could not be extracted from PDF")
                source = await db.get(Source, batch_id)
                if source:
                    source.status = 'failed'
                    source.source_metadata['error'] = f"MPN {mpn} not found in PDF"
                    source.source_metadata['failed'] = 1
                    source.source_metadata['completed_at'] = datetime.utcnow().isoformat()
                    source.source_metadata['processing_status'] = 'failed'
                    db.add(source)
                    await db.commit()
                    await sync_project_status(db, project_id)
                    
                return
            if hasattr(product_info, 'model_dump'):
                prod_dict = product_info.model_dump()
            elif hasattr(product_info, 'dict'):
                prod_dict = product_info.dict()
            elif isinstance(product_info, dict):
                prod_dict = product_info
            else:
                raise ValueError(f"Unexpected product_info type: {type(product_info)}")
            spec_count = len(prod_dict.get('specifications', {}))
            completeness_score = min(spec_count * 5, 100)
            enrichment_threshold = getattr(settings, 'enrichment_threshold', 90)
            if completeness_score < enrichment_threshold:
                workflow_stage = 'enrichment'
                needs_enrichment = True
                ready_for_export = False
                routed_to_enrichment_at = datetime.utcnow()
                enrichment_status = 'pending'
            else:
                workflow_stage = 'aggregation'
                needs_enrichment = False
                ready_for_export = True
                routed_to_enrichment_at = None
                enrichment_status = 'completed'
            existing_product = await db.execute(
            select(Product).where(
                Product.project_id == project_id,
                Product.product_code == mpn
            )
        )
            existing_product = existing_product.scalar_one_or_none()
            if existing_product:
                existing_product.product_name = prod_dict.get('product_name', f"Product {mpn}")
                existing_product.brand_name = prod_dict.get('brand_name', "")
                existing_product.sku = prod_dict.get('sku', "")
                existing_product.taxonomy = prod_dict.get('taxonomy', '')
                existing_product.description = prod_dict.get('description', '')
                existing_product.image_url_1 = prod_dict.get('image_url', '')
                existing_product.workflow_stage = workflow_stage
                existing_product.needs_enrichment = needs_enrichment
                existing_product.ready_for_export = ready_for_export
                existing_product.routed_to_enrichment_at = routed_to_enrichment_at
                existing_product.enrichment_status = enrichment_status
                existing_product.attributes = prod_dict.get('specifications', {})
                existing_product.source_url = filename
                existing_product.completeness_score = completeness_score
                db.add(existing_product)
            else:
                product = Product(
                    product_code=mpn,
                    product_name=prod_dict.get('product_name', f"Product {mpn}"),
                    brand_name=prod_dict.get('brand_name', ""),
                    mpn=mpn,
                    sku=prod_dict.get('sku', ""),
                    taxonomy=prod_dict.get('taxonomy', ''),
                    description=prod_dict.get('description', ''),
                    image_url_1=prod_dict.get('image_url', ''),
                    project_id=project_id,
                    workflow_stage=workflow_stage,
                    needs_enrichment=needs_enrichment,
                    ready_for_export=ready_for_export,
                    routed_to_enrichment_at=routed_to_enrichment_at,
                    enrichment_status=enrichment_status,
                    attributes=prod_dict.get('specifications', {}),
                    source_url=filename,
                    completeness_score=completeness_score
                )
                db.add(product)
            await db.flush()
            source = await db.get(Source, batch_id)
            if source:
                source.status = 'completed'
                source.source_metadata.update({
                    'successful': 1,
                    'failed': 0,
                    'product': prod_dict,
                    'mpn': mpn,
                    'completeness_score': completeness_score,
                    'workflow_stage': workflow_stage,
                    'completed_at': datetime.utcnow().isoformat(),
                    'processing_status': 'completed'
                })
                db.add(source)
            await db.commit()
            await sync_project_status(db, project_id)
            logger.info(f"Structured extraction completed for MPN {mpn} (score {completeness_score}, stage {workflow_stage})")
        except Exception as e:
            logger.error(f"Structured PDF extraction failed for batch {batch_id}: {e}", exc_info=True)
            await db.rollback()
            source = await db.get(Source, batch_id)
            if source:
                source.status = 'failed'
                source.source_metadata['error'] = str(e)
                source.source_metadata['processing_status'] = 'failed'
                db.add(source)
                await db.commit()
                await sync_project_status(db, project_id)
import os
from app.core.config import settings
@router.post('/save-pdf-source')
async def save_pdf_source(
    file: UploadFile = File(...),
    mpn: str = Form(...),
    project_id: str = Form(...),
    use_case: str = Form(...),
    db: AsyncSession = Depends(get_session)
):
    try:
        batch_id = str(uuid.uuid4())
        pdf_bytes = await file.read()
        MAX_FILE_SIZE = 20 * 1024 * 1024  
        if len(pdf_bytes) > MAX_FILE_SIZE:
            raise HTTPException(400, f"File size exceeds {MAX_FILE_SIZE // (1024*1024)} MB limit")
        source = Source(
            id=batch_id,
            source_type="pdf_pending_extraction",
            source_url=file.filename,
            project_id=project_id,
            status="pending",
            content_data=pdf_bytes,  
            source_metadata={
                "mpn": mpn,
                "use_case": use_case,
                "extracted": False,
                "processing_status": "pending"
            }
        )
        db.add(source)
        stmt = select(Product).where(
            Product.project_id == project_id,
            Product.product_code == mpn
        )
        result = await db.execute(stmt)
        existing = result.scalar_one_or_none()
        if not existing:
            product = Product(
                product_code=mpn,
                product_name=f"Pending extraction: {mpn}",
                mpn=mpn,
                project_id=project_id,
                workflow_stage="aggregation",
                enrichment_status="pending",
                source_url=file.filename,
                completeness_score=0
            )
            db.add(product)
        logger.info(f"Saving source with content_data size: {len(pdf_bytes)} bytes")
        await db.commit()
        saved_source = await db.get(Source, batch_id)
        logger.info(
    f"Verified content_data size: {len(saved_source.content_data) if saved_source.content_data else 0} bytes"
)
        return {
            "status": "success",
            "batch_id": batch_id,
            "message": f"PDF saved for MPN {mpn}"
        }
    except Exception as e:
        logger.error(f"Failed to save PDF source: {e}")
        raise HTTPException(500, str(e))
@router.post('/extract-pending')
async def extract_pending_pdf(
    data: dict,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session)
):
    try:
        mpn = data.get('mpn')
        project_id = data.get('project_id')
        if not mpn or not project_id:
            raise HTTPException(400, "Missing required fields: mpn and project_id")
        stmt = select(Source).where(
            Source.source_type == "pdf_pending_extraction",
            func.json_extract_path_text(Source.source_metadata, 'mpn') == mpn,
            Source.project_id == project_id
        )
        result = await db.execute(stmt)
        source = result.scalar_one_or_none()
        if not source:
            raise HTTPException(404, f"PDF source not found for MPN: {mpn}")
        source.status = "processing"
        source.source_metadata["processing_status"] = "processing"
        db.add(source)
        await db.commit()
        background_tasks.add_task(
            process_pdf_extraction_for_product,
            source.id, mpn, project_id
        )
        return {"status": "processing", "batch_id": source.id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to start pending PDF extraction for MPN {mpn}: {e}", exc_info=True)
        raise HTTPException(500, f"Internal server error: {str(e)}")
async def process_pdf_extraction_for_product(
    batch_id: str,
    mpn: str,
    project_id: str
) -> None:
    async with async_session_factory() as db:
        try:
            source = await db.get(Source, batch_id)
            if not source or source.source_type != "pdf_pending_extraction":
                logger.error(f"Invalid source for batch {batch_id}")
                return
            if source.source_metadata.get("extracted"):
                logger.info(f"PDF for {mpn} already extracted, skipping")
                return
            pdf_bytes = source.content_data
            if not pdf_bytes:
                raise ValueError("No PDF content found in source")
            await process_structured_pdf_extraction(
                batch_id, pdf_bytes, mpn, project_id, source.source_url
            )
            source.source_metadata["extracted"] = True
            source.source_metadata["processing_status"] = "completed"
            source.content_data = None  
            source.status = "completed"
            db.add(source)
            await db.commit()
            await sync_project_status(db, project_id)
        except Exception as e:
            logger.error(f"Failed to extract pending PDF for {mpn}: {e}")
            source = await db.get(Source, batch_id)
            if source:
                source.status = "failed"
                source.source_metadata["error"] = str(e)
                source.source_metadata["processing_status"] = "failed"
                db.add(source)
                await db.commit()
                await sync_project_status(db, project_id)