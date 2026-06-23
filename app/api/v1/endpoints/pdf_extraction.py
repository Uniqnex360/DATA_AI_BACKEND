import os
from io import BytesIO
from fastapi.responses import StreamingResponse
import pdfplumber
import json
import pickle
from sqlalchemy import update as sa_update
import re
from app.aggregation.services.pdf_prompt_service import PDFPromptService
from app.core.config import settings
from sqlmodel import select
from sqlalchemy import func, case
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm.attributes import flag_modified
from datetime import datetime, timedelta, timezone
import aiohttp
from app.aggregation.services.pdf_service import PDFExtractionService
from app.core.database import get_session
from app.llm import call_llm_with_schema
from app.models.pipeline import Source
from app.models.product import Product
from app.models.project import Project
from app.models.project_product_link import ProjectProductLink
from app.schemas.pdf_extraction import FreshAggregationRequest, PDFExtractionResponse
import uuid
import logging
from typing import List, Optional, Dict
from app.search.searxng_service import SearXNGSearchService
from app.core.database import async_session_factory
from app.services.excel_mpn_service import ExcelMPNService
from app.utils.pdf_helpers import extract_pdf_text, score_pdf_text_for_mpn, slice_text_around_mpn
from app.utils.timezone import now_ist
logger = logging.getLogger('pdf extraction')
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
    ).join(
        ProjectProductLink, Product.id == ProjectProductLink.product_id  
    ).where(
        ProjectProductLink.project_id == project_id  
    )
    result = await db.execute(stmt)
    row = result.one()
    total = row[0] or 0
    completed = row[1] or 0
    failed = row[2] or 0
    processing = row[3] or 0
    pending = row[4] or 0
    source_stmt = select(Source).where(
        Source.project_id == project_id,
        Source.status == "failed"
    )
    source_result = await db.execute(source_stmt)
    failed_source = source_result.scalars().first()
    if total == 0:
        if failed_source:
            project.status = "failed"
        else:
            project.status = "draft"
    elif processing > 0:
        project.status = "processing"
    elif completed == total:
        project.status = "completed"
    elif failed == total:
        project.status = "failed"
    elif completed > 0 or pending > 0:
        project.status = "partially_completed"
    else:
        project.status = "draft"
    db.add(project)
    await db.commit()
    logger.info(f"Project {project_id} status updated to: {project.status}")


@router.post("/fresh-aggregation")
async def fresh_aggregation(
    request: FreshAggregationRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session),
):
    try:
        stmt = select(Source).where(
            Source.project_id == request.project_id,
            Source.source_type == "pdf_fresh_pending",
            Source.status == "pending",
        )
        result = await db.execute(stmt)
        existing_source = result.scalar_one_or_none()
        if existing_source:
            batch_id = existing_source.id
            existing_source.status = "processing"
            existing_source.source_metadata["processing_status"] = "processing"
            existing_source.source_metadata["started_at"] = now_ist(
            ).isoformat()
            flag_modified(existing_source, "source_metadata")
            db.add(existing_source)
        else:
            batch_id = str(uuid.uuid4())
            source = Source(
                id=batch_id,
                source_type="pdf_fresh_aggregation",
                source_url="web_enrichment",
                project_id=request.project_id,
                status="processing",
                source_metadata={
                    "mpns": request.mpns,
                    "use_case": request.use_case,
                    "started_at": now_ist().isoformat(),
                    "total": len(request.mpns),
                    "successful": 0,
                    "failed": 0,
                    "processing_status": "processing",
                },
            )
            db.add(source)
        if request.mpns:
            await db.execute(
                sa_update(Product)
                .join(
                    ProjectProductLink, Product.id == ProjectProductLink.product_id  
                )
                .where(
                    ProjectProductLink.project_id == request.project_id,  
                    Product.product_code.in_(request.mpns),
                    Product.enrichment_status.in_(["pending", "failed"]),
                )
                .values(enrichment_status="processing")
            )
        await db.commit()
        background_tasks.add_task(
            process_fresh_pdf_aggregation,
            batch_id,
            request.mpns,
            request.project_id,
        )
        return {
            "status": "processing",
            "batch_id": batch_id,
            "message": f"Processing {len(request.mpns)} MPN(s)",
        }
    except Exception as e:
        logger.error(f"Failed to start fresh aggregation: {e}")
        raise HTTPException(500, str(e))


async def search_pdfs_url(mpn: str, brand: str) -> List[str]:
    try:
        logger.info(f"🔍 [1/4] Starting PDF search for MPN: {mpn}")
        searxng = SearXNGSearchService(
            base_url=f"{settings.SEARXNG_URL}/search", max_results=20)
        search_queries = [
            f"{mpn} datasheet pdf",
            f"{mpn} specification sheet pdf",
            f"{mpn} data sheet pdf",
            f"{brand} {mpn} product pdf" if brand else f"{mpn} product pdf"
        ]
        logger.info(f" Search queries: {search_queries}")
        pdf_urls = []
        for query in search_queries:
            logger.info(f" Executing search: {query}")
            try:
                results = await searxng._search(query)
                logger.info(
                    f"   Found {len(results)} results for query: {query}")
                for result in results:
                    url = result.get('url', '')
                    if url and url.lower().endswith('.pdf'):
                        pdf_urls.append(url)
                        logger.info(f"PDF found: {url}")
                    else:
                        logger.debug(f"Skipped non-PDF: {url[:80]}...")
            except Exception as e:
                logger.debug(f"Search failed for {query}: {e}")
        return list(set(pdf_urls))[:5]
    except Exception as e:
        logger.error(f"Failed to search pdf urls: {e}")
        raise HTTPException(500, str(e))


async def download_and_extract_pdf(pdf_url: str) -> Optional[str]:
    pdf_service = PDFExtractionService(max_pages=15)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(pdf_url, timeout=30) as resp:
                if resp.status == 200:
                    pdf_bytes = await resp.read()
                    text = await pdf_service.extract_text(pdf_bytes)
                    if text and len(text.strip()) > 200:
                        logger.info(
                            f'Extracted {len(text)} chars from {pdf_url}')
                        return text
    except Exception as e:
        logger.warning(f"Failed to download/extract PDF {pdf_url}: {e}")
    return None


@router.post('/blind-pdf-extraction')
async def blind_pdf_extraction(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    project_id: str = Form(...),
    use_case: str = Form(...),
    product_hint: str = Form(...),
    db: AsyncSession = Depends(get_session)
):
    try:
        if len(files) == 0:
            raise HTTPException(400, "At least one PDF file is required")
        if len(files) > 10:
            raise HTTPException(400, "Maximum 10 PDF files allowed")
        batch_id = str(uuid.uuid4())
        pdf_documents = []
        total_size = 0
        MAX_TOTAL_SIZE = 50 * 1024 * 1024
        if not product_hint or not product_hint.strip():
            raise HTTPException(400, "Product hint is required")
        for file in files:
            if not file.filename.lower().endswith('.pdf'):
                raise HTTPException(
                    400, f"File '{file.filename}' is not a PDF")
            pdf_bytes = await file.read()
            total_size += len(pdf_bytes)
            if total_size > MAX_TOTAL_SIZE:
                raise HTTPException(
                    400, f"Total file size exceeds {MAX_TOTAL_SIZE // (1024*1024)} MB")
            pdf_documents.append({
                'filename': file.filename,
                'content': pdf_bytes,
                'size': len(pdf_bytes)
            })
        source = Source(
            id=batch_id,
            source_type="pdf_blind_extraction",
            source_url=f"blind_pdf_{len(pdf_documents)}_files",
            project_id=project_id,
            status="processing",
            content_data=pickle.dumps(pdf_documents),
            source_metadata={
                "use_case": use_case,
                "pdf_files": [{'filename': p['filename'], 'size': p['size']} for p in pdf_documents],
                "total_pdfs": len(pdf_documents),
                "created_at": now_ist().isoformat(),
                "processing_status": "processing",
                "extraction_type": "blind",
                "product_hint": product_hint.strip(),
            }
        )
        db.add(source)
        await db.commit()
        background_tasks.add_task(
            process_blind_pdf_extraction,
            batch_id,
            project_id
        )
        return {
            "status": "processing",
            "batch_id": batch_id,
            "message": f"Processing {len(pdf_documents)} PDF(s). Products will appear in Aggregation tab when ready.",
            "pdfs_count": len(pdf_documents)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to start blind PDF extraction: {e}", exc_info=True)
        raise HTTPException(500, str(e))


async def extract_product_with_claude(mpn: str, pdf_text: str, source_url: str, db: AsyncSession = None, project_id: str = None) -> Optional[Dict]:
    try:
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        prompt_service = PDFPromptService(db, project_id)
        prompt = await prompt_service.get_extraction_prompt(
            pdf_text, mpn, "Fresh PDF Aggregation", is_unstructured=False
        )
        if not prompt:
            error_msg = "No business rule prompt configured for PDF Extraction"
            logger.warning(f"{error_msg} for project {project_id}")
            return None

        extracted = await call_llm_with_schema(prompt=prompt, response_model="PDFExtractionResponse", llm_provider='claude', estimated_tokens=4000)
        if extracted:
            result = extracted.model_dump() if hasattr(
                extracted, 'model_dump') else extracted.dict()
            result['mpn'] = mpn
            result['source_url'] = source_url
            return result
    except Exception as e:
        logger.error(f"Claude extraction failed for {mpn}: {e}")
    return None


async def process_blind_pdf_extraction(
    batch_id: str,
    project_id: str
) -> None:
    async with async_session_factory() as db:
        try:
            source = await db.get(Source, batch_id)
            if not source:
                logger.error(f"Source not found: {batch_id}")
                return
            product_hint = source.source_metadata.get("product_hint")
            if not product_hint:
                logger.error("No product hint found in source metadata")
                return
            pdf_documents = pickle.loads(
                source.content_data) if source.content_data else []
            if not pdf_documents:
                raise ValueError("No PDFs found in source")
            source.status = "processing"
            source.source_metadata["processing_status"] = "processing"
            flag_modified(source, "source_metadata")
            db.add(source)
            await db.commit()
            logger.info(
                f" Processing blind extraction: {len(pdf_documents)} PDFs")
            all_extracted_products = []
            successful = 0
            failed = 0
            for pdf_doc in pdf_documents:
                try:
                    logger.info(f"📄 Processing: {pdf_doc['filename']}")
                    full_text = ""
                    with pdfplumber.open(BytesIO(pdf_doc['content'])) as pdf:
                        for page in pdf.pages[:20]:
                            page_text = page.extract_text()
                            if page_text:
                                full_text += page_text + "\n"
                    if not full_text.strip():
                        logger.warning(
                            f"No text extracted from {pdf_doc['filename']}")
                        failed += 1
                        continue
                    products_found = await identify_products_in_pdf(
                        full_text[:20000],
                        pdf_doc['filename'],
                        product_hint,
                        db,
                        project_id
                    )
                    if not products_found:
                        logger.warning(
                            f"No products identified in {pdf_doc['filename']}")
                        failed += 1
                        continue
                    logger.info(
                        f"   Found {len(products_found)} potential products")
                    for product_info in products_found:
                        try:
                            if isinstance(product_info, str):
                                product_info = {
                                    "title": product_info, "context": "", "confidence": 0.7}
                            if not isinstance(product_info, dict):
                                logger.warning(
                                    f"Skipping non-dict product_info: {type(product_info)}")
                                continue
                            extracted = await extract_blind_product_details(
                                full_text[:15000],
                                product_info,
                                db,
                                project_id
                            )
                            if extracted:
                                auto_mpn = generate_mpn_from_title(
                                    extracted.get('product_name', 'Unknown'))
                                product = await upsert_product_from_extraction(
                                    db, project_id, auto_mpn, extracted, pdf_doc['filename']
                                )
                                all_extracted_products.append({
                                    'mpn': auto_mpn,
                                    'name': extracted.get('product_name'),
                                    'source': pdf_doc['filename'],
                                    'completeness': product.completeness_score
                                })
                                successful += 1
                                logger.info(
                                    f"Extracted: {extracted.get('product_name')} (score: {product.completeness_score}%)")
                        except Exception as e:
                            logger.error(
                                f"Failed to extract product: {e}")
                            continue
                except Exception as e:
                    logger.error(
                        f"   Failed to process {pdf_doc['filename']}: {e}")
                    failed += 1
            source = await db.get(Source, batch_id)
            if source:
                source.status = "completed" if successful > 0 else "failed"
                source.source_metadata.update({
                    "processing_status": "completed" if successful > 0 else "failed",
                    "successful": successful,
                    "failed": failed,
                    "extracted_products": all_extracted_products,
                    "completed_at": now_ist().isoformat()
                })
                flag_modified(source, "source_metadata")
                db.add(source)
                await db.commit()
                await db.refresh(source)
                await sync_project_status(db, project_id)
                logger.info(f" Source {batch_id} updated to {source.status}")
            await sync_project_status(db, project_id)
            logger.info(
                f" Blind extraction completed: {successful} products extracted")
        except Exception as e:
            logger.error(f" Blind extraction failed: {e}", exc_info=True)
            await db.rollback()
            source = await db.get(Source, batch_id)
            if source:
                source.status = "failed"
                source.source_metadata["error"] = str(e)
                source.source_metadata["processing_status"] = "failed"
                db.add(source)
                await db.commit()
                await sync_project_status(db, project_id)


async def identify_products_in_pdf(pdf_text: str, filename: str, product_hint: str, db: AsyncSession = None, project_id: str = None) -> List[Dict]:
    text_lower = pdf_text.lower()
    hint_lower = product_hint.lower()
    if hint_lower not in text_lower:
        logger.info(
            f"Hint '{product_hint}' NOT found in document '{filename}', skipping extraction")
        return []
    logger.info(
        f" Hint '{product_hint}' found in document, proceeding with Claude extraction")
    prompt_service = PDFPromptService(db, project_id)
    prompt = await prompt_service.get_identification_prompt(
        pdf_text, filename, product_hint, "Title & Description Based PDF Extraction"
    )
    if not prompt:
        error_msg = "No business rule prompt configured for Blind PDF Extraction"
        logger.warning(f"{error_msg} for project {project_id}")
        return None

    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    try:
        response = await call_llm_with_schema(
            prompt=prompt,
            response_model="ProductIdentificationResponse",
            llm_provider='claude',
            estimated_tokens=3000
        )
        if response is None:
            return []
        if hasattr(response, 'root'):
            result = response.root
        elif hasattr(response, 'model_dump'):
            result = response.model_dump()
        elif isinstance(response, list):
            result = response
        else:
            logger.warning(f"Unexpected response type: {type(response)}")
            return []
        validated = []
        for item in result:
            if isinstance(item, str):
                validated.append(
                    {"title": item, "context": f"Found in {filename}", "confidence": 0.7})
            elif isinstance(item, dict):
                if 'context' not in item:
                    item['context'] = f"Found in {filename}"
                if 'confidence' not in item:
                    item['confidence'] = 0.7
                validated.append(item)
        return validated
    except Exception as e:
        logger.error(f"Product identification failed: {e}", exc_info=True)
        return []


async def extract_blind_product_details(pdf_text: str, product_info: Dict, db: AsyncSession = None, project_id: str = None) -> Optional[Dict]:
    title = product_info.get('title', 'Unknown Product')
    context = product_info.get('context', '')
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    prompt_service = PDFPromptService(db, project_id)
    prompt = await prompt_service.get_blind_extraction_prompt(
        pdf_text, product_info, "Title & Description Based PDF Extraction"
    )
    if not prompt:
        error_msg = "No business rule prompt configured for PDF Identification"
        logger.warning(f"{error_msg} for project {project_id}")
        return []
    try:
        response = await call_llm_with_schema(
            prompt=prompt,
            response_model="PDFExtractionResponse",
            llm_provider='claude',
            estimated_tokens=4000
        )
        return parse_llm_response(response, title)
    except Exception as e:
        logger.error(f"Blind product extraction failed for {title}: {e}")
        return None


def generate_mpn_from_title(title: str) -> str:
    slug = re.sub(r'[^a-zA-Z0-9]', '-', title.lower())
    slug = re.sub(r'-+', '-', slug).strip('-')
    timestamp = now_ist().strftime('%Y%m%d%H%M%S')
    return f"{slug[:30]}-{timestamp[-6:]}"


async def process_fresh_pdf_aggregation(batch_id: str, mpns: List[str], project_id: str, detailed_data: List[Dict] = None):
    try:
        async with async_session_factory() as db:
            try:
                context_map = {}
                if detailed_data:
                    for item in detailed_data:
                        context_map[item.get('mpn', '')] = {
                            'brand': item.get('brand', ''),
                            'product_name': item.get('product_name', '')
                        }
                products = []
                successful = 0
                routed_to_enrichment_count = 0
                ready_for_export_count = 0
                enrichment_threshold = settings.enrichment_threshold
                failed_mpns = []
                for idx, mpn in enumerate(mpns):
                    try:
                        context = context_map.get(
                            mpn, {}) if context_map else {}
                        brand = context.get('brand', '')
                        logger.info(
                            f"Processing MPN {idx+1}/{len(mpns)}: {mpn}")
                        stmt = select(Product).join(
                            ProjectProductLink, Product.id == ProjectProductLink.product_id  
                        ).where(
                            Product.product_code == mpn
                        )
                        result = await db.execute(stmt)
                        existing_product = result.scalar_one_or_none()
                        pdf_urls = await search_pdfs_url(mpn, brand)
                        if not pdf_urls:
                            logger.warning(f"No PDF urls found for {mpn}")
                            failed_mpns.append(mpn)
                            if existing_product:
                                existing_product.enrichment_status = "failed"
                                db.add(existing_product)
                            else:
                                product = Product(
                                    product_code=mpn,
                                    product_name=f"Failed: {mpn}",
                                    mpn=mpn,
                                    workflow_stage='aggregation',
                                    enrichment_status='failed',
                                    source_url='web_search_failed',
                                    completeness_score=0
                                )
                                db.add(product)
                                await db.flush()
                                db.add(ProjectProductLink(
                                    project_id=project_id, product_id=product.id))
                            continue
                        product_data = None
                        for pdf_url in pdf_urls:
                            pdf_text = await download_and_extract_pdf(pdf_url)
                            if pdf_text:
                                product_data = await extract_product_with_claude(mpn, pdf_text, pdf_url, db, project_id)
                                if product_data:
                                    break
                        if product_data:
                            specifications = product_data.get(
                                'specifications', {})
                            spec_count = len(specifications)
                            completeness_score = min(spec_count * 5, 100)
                            if completeness_score < enrichment_threshold:
                                workflow_stage = 'enrichment'
                                needs_enrichment = True
                                is_ready_for_export = False
                                routed_to_enrichment_at = now_ist()
                                enrichment_status = 'pending'
                                routed_to_enrichment_count += 1
                            else:
                                workflow_stage = 'aggregation'
                                needs_enrichment = False
                                is_ready_for_export = True
                                routed_to_enrichment_at = None
                                enrichment_status = 'completed'
                                ready_for_export_count += 1
                            if existing_product:
                                existing_product.product_name = product_data.get(
                                    'product_name', existing_product.product_name)
                                existing_product.brand_name = product_data.get(
                                    "brand_name", existing_product.brand_name)
                                existing_product.sku = product_data.get(
                                    "sku", existing_product.sku)
                                existing_product.taxonomy = product_data.get(
                                    'taxonomy', existing_product.taxonomy)
                                existing_product.description = product_data.get(
                                    'description', existing_product.description)
                                existing_product.image_url_1 = product_data.get(
                                    'image_url', existing_product.image_url_1)
                                existing_product.workflow_stage = workflow_stage
                                existing_product.enrichment_status = enrichment_status
                                existing_product.ready_for_export = is_ready_for_export
                                existing_product.needs_enrichment = needs_enrichment
                                existing_product.routed_to_enrichment_at = routed_to_enrichment_at
                                existing_product.attributes = specifications
                                existing_product.source_url = product_data.get(
                                    'source_url', existing_product.source_url)
                                existing_product.completeness_score = completeness_score
                                db.add(existing_product)
                            else:
                                product = Product(
                                    product_code=mpn,
                                    product_name=product_data.get(
                                        'product_name', f"Product {mpn}"),
                                    brand_name=product_data.get(
                                        "brand_name", ""),
                                    mpn=mpn,
                                    sku=product_data.get("sku", ""),
                                    taxonomy=product_data.get('taxonomy', ''),
                                    description=product_data.get(
                                        'description', ''),
                                    image_url_1=product_data.get('image_url'),
                                    workflow_stage=workflow_stage,
                                    enrichment_status=enrichment_status,
                                    ready_for_export=is_ready_for_export,
                                    needs_enrichment=needs_enrichment,
                                    routed_to_enrichment_at=routed_to_enrichment_at,
                                    attributes=specifications,
                                    source_url=product_data.get(
                                        'source_url', ""),
                                    completeness_score=completeness_score
                                )
                                db.add(product)
                                await db.flush()
                                db.add(ProjectProductLink(
                                    project_id=project_id, product_id=product.id))
                            products.append(product_data)
                            successful += 1
                            logger.info(
                                f" Successfully extracted product for {mpn} (score {completeness_score})")
                        else:
                            failed_mpns.append(mpn)
                            logger.warning(
                                f"Failed to extract product for {mpn}")
                            if existing_product:
                                existing_product.enrichment_status = "failed"
                                db.add(existing_product)
                            else:
                                product = Product(
                                    product_code=mpn,
                                    product_name=f"Failed: {mpn}",
                                    mpn=mpn,
                                    workflow_stage='aggregation',
                                    enrichment_status='failed',
                                    source_url='web_search_failed',
                                    completeness_score=0
                                )
                                db.add(product)
                                await db.flush()
                                db.add(ProjectProductLink(
                                    project_id=project_id, product_id=product.id))
                    except Exception as e:
                        logger.error(f"Error processing MPN {mpn}: {e}")
                        failed_mpns.append(mpn)
                        stmt = select(Product).join(
                            ProjectProductLink, Product.id == ProjectProductLink.product_id  
                        ).where(
                            Product.product_code == mpn
                        )
                        result = await db.execute(stmt)
                        existing = result.scalar_one_or_none()
                        if existing:
                            existing.enrichment_status = "failed"
                            db.add(existing)
                        else:
                            product = Product(
                                product_code=mpn,
                                product_name=f"Error: {mpn}",
                                mpn=mpn,
                                workflow_stage='aggregation',
                                enrichment_status='failed',
                                source_url='web_search_error',
                                completeness_score=0
                            )
                            db.add(product)
                            await db.flush()
                            db.add(ProjectProductLink(
                                project_id=project_id, product_id=product.id))
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
                            flag_modified(source, "source_metadata")
                            db.add(source)
                            await db.commit()
                            await sync_project_status(db, project_id)
                source = await db.get(Source, batch_id)
                if source:
                    source.status = 'completed' if successful > 0 else 'failed'
                    source.source_metadata.update({
                        'completed_at': now_ist().isoformat(),
                        'products': products,
                        'failed_mpns': failed_mpns,
                        'routed_to_enrichment': routed_to_enrichment_count,
                        'processing_status': 'completed' if successful > 0 else 'failed',
                        'ready_for_export': ready_for_export_count
                    })
                    flag_modified(source, "source_metadata")
                    db.add(source)
                    await db.commit()
                    await sync_project_status(db, project_id)
                logger.info(
                    f"Fresh aggregation completed: {successful} successful, {len(failed_mpns)} failed")
            except Exception as e:
                logger.error(f"Fresh PDF aggregation failed: {e}")
                await db.rollback()
                source = await db.get(Source, batch_id)
                if source:
                    source.status = 'failed'
                    source.source_metadata['error'] = str(e)
                    source.source_metadata['processing_status'] = 'failed'
                    db.add(source)
                    await db.commit()
                    await sync_project_status(db, project_id)
    except Exception as e:
        logger.error(f"Fresh PDF aggregation outer failed: {e}")


@router.get('/batch-status/{batch_id}')
async def get_batch_status(batch_id: str, db: AsyncSession = Depends(get_session)):
    try:
        source = await db.get(Source, batch_id)
        if not source:
            raise HTTPException(404, "Batch not found")
        return {
            'status': source.status,
            'source_metadata': source.source_metadata,
            'created_at': source.created_at,
            'updated_at': source.updated_at
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
        batch_id = str(uuid.uuid4())
        pdf_bytes = await file.read()
        source = Source(
            id=batch_id,
            source_type='pdf_structured_extraction',
            source_url=file.filename,
            project_id=project_id,
            status='processing',
            source_metadata={
                'mpn': mpn,
                'use_case': use_case,
                'started_at': now_ist().isoformat(),
                'processing_status': 'processing',
                'successful': 0,
                'failed': 0
            }
        )
        db.add(source)
        await db.commit()
        background_tasks.add_task(
            process_structured_pdf_extraction, batch_id, pdf_bytes, mpn, project_id, file.filename)
        return {
            'status': 'processing',
            'batch_id': batch_id,
            'message': f'Processing MPN {mpn} from PDF'
        }
    except Exception as e:
        logger.error(f"Failed to start structured extraction: {e}")
        raise HTTPException(500, str(e))


async def process_structured_pdf_extraction(
    batch_id: str,
    pdf_bytes: bytes,
    mpn: str,
    project_id: str,
    filename: str
) -> None:
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
                            headers = [
                                str(h).strip() if h else "" for h in tbl[0]]
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
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            prompt_service = PDFPromptService(db, project_id)
            prompt = await prompt_service.get_structured_extraction_prompt(
                truncated_text, truncated_tables, mpn, "Structured PDF Extraction (Given MPNs)"
            )
            if not prompt:
                error_msg = "No business rule prompt configured for Structured PDF Extraction"
                logger.warning(f"{error_msg} for project {project_id}")

                source = await db.get(Source, batch_id)
                if source:
                    source.status = "failed"
                    source.source_metadata.update({
                        "processing_status": "failed",
                        "error": error_msg,
                        "error_type": "missing_prompt",
                        "failed_at": now_ist().isoformat()
                    })
                    flag_modified(source, "source_metadata")
                    db.add(source)
                    await db.commit()
                    await sync_project_status(db, project_id)

                
                stmt = select(Product).join(
                    ProjectProductLink, Product.id == ProjectProductLink.product_id  
                ).where(
                    Product.product_code == mpn
                )
                result = await db.execute(stmt)
                product = result.scalar_one_or_none()
                if product:
                    product.enrichment_status = "failed"
                    db.add(product)
                    await db.commit()

                return
            logger.info(
                f"Extracted text length: {len(full_text)}, tables: {len(tables)}")
            response = await call_llm_with_schema(
                prompt=prompt,
                response_model="PDFExtractionResponse",
                llm_provider='claude',
                estimated_tokens=4000
            )
            prod_dict = parse_llm_response(response, mpn)
            if not prod_dict:
                raise ValueError(f"Could not extract data for MPN {mpn}")
            product = await upsert_product_from_extraction(db, project_id, mpn, prod_dict, filename)
            source = await db.get(Source, batch_id)
            if source:
                source.status = 'completed'
                source.source_metadata.update({
                    'successful': 1,
                    'failed': 0,
                    'completeness_score': product.completeness_score,
                    'processing_status': 'completed',
                    'completed_at': now_ist().isoformat()
                })
                flag_modified(source, "source_metadata")
                db.add(source)
            await db.commit()
            await sync_project_status(db, project_id)
            logger.info(
                f"Structured extraction completed for {mpn} (score {product.completeness_score})")
        except Exception as e:
            logger.error(f"Structured extraction failed: {e}", exc_info=True)
            await db.rollback()
            source = await db.get(Source, batch_id)
            if source:
                source.status = 'failed'
                source.source_metadata['error'] = str(e)
                source.source_metadata['processing_status'] = 'failed'
                db.add(source)
                await db.commit()
                await sync_project_status(db, project_id)


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
            raise HTTPException(
                400, f"File size exceeds {MAX_FILE_SIZE // (1024*1024)} MB limit")
        is_unstructured = 'Unstructured PDF Extraction' in use_case
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
                "processing_status": "pending",
                "is_unstructured": is_unstructured
            }
        )
        db.add(source)
        stmt = select(Product).join(
            ProjectProductLink, Product.id == ProjectProductLink.product_id  
        ).where(
            Product.product_code == mpn
        )
        result = await db.execute(stmt)
        existing = result.scalar_one_or_none()
        if not existing:
            product = Product(
                product_code=mpn,
                product_name=f"{mpn}",
                mpn=mpn,
                workflow_stage="aggregation",
                enrichment_status="pending",
                source_url=file.filename,
                completeness_score=0
            )
            db.add(product)
            await db.flush()
            db.add(ProjectProductLink(
                project_id=project_id, product_id=product.id))
        logger.info(
            f"Saving source with content_data size: {len(pdf_bytes)} bytes")
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


async def process_multi_pdf_extraction_for_single_mpn(
    batch_id: str,
    mpn: str,
    project_id: str
) -> None:
    async with async_session_factory() as db:
        try:
            source = await db.get(Source, batch_id)
            if not source:
                logger.error(f"Source not found: {batch_id}")
                return
            pdf_documents = pickle.loads(
                source.content_data) if source.content_data else []
            if not pdf_documents:
                raise ValueError("No PDFs found in source")
            logger.info(
                f"📄 Processing MPN {mpn} from multi-PDF source with {len(pdf_documents)} PDFs")
            pdf_texts = []
            for pdf_doc in pdf_documents:
                try:
                    full_text = extract_pdf_text(
                        pdf_doc['content'], max_pages=60)
                    pdf_texts.append({
                        'filename': pdf_doc['filename'],
                        'text': full_text,
                        'length': len(full_text)
                    })
                    logger.info(
                        f"   Extracted {len(full_text)} chars from {pdf_doc['filename']}")
                    logger.info(
                        f"=== PDF CONTENT FROM {pdf_doc['filename']} ===")
                    logger.info(f"Total text length: {len(full_text)}")
                    logger.info(f"First 1000 chars:\n{full_text[:1000]}")
                    logger.info(f"Last 1000 chars:\n{full_text[-1000:]}")
                    text_lower = full_text.lower()
                    mpn_lower = str(mpn).lower()
                    mpn_float = str(float(mpn)) if mpn.replace(
                        '.', '').isdigit() else mpn
                    logger.info(f"Looking for MPN: '{mpn}'")
                    logger.info(f"MPN as float: '{mpn_float}'")
                    logger.info(f"MPN in text (exact): {mpn in full_text}")
                    logger.info(
                        f"MPN lowercase in text: {mpn_lower in text_lower}")
                    logger.info(f"MPN float in text: {mpn_float in full_text}")
                    all_numbers = re.findall(r'\d{4,}', full_text)
                    logger.info(
                        f"All 4+ digit numbers in PDF: {all_numbers[:20]}")
                except Exception as e:
                    logger.error(
                        f"   Failed to extract {pdf_doc['filename']}: {e}")
            best_pdf = None
            best_score = 0
            for pdf_text in pdf_texts:
                if not pdf_text['text']:
                    continue
                score = score_pdf_text_for_mpn(
                    pdf_text["text"], pdf_text["filename"], mpn)
                logger.info(f"Match score for {pdf_text['filename']}: {score}")
                if score > best_score:
                    best_score = score
                    best_pdf = pdf_text
            if best_pdf and best_score > 0:
                logger.info(
                    f"Best match: {best_pdf['filename']} (score: {best_score})")
                truncated_text = slice_text_around_mpn(
                    best_pdf["text"], mpn, window=15000, back=6000)
                
                
                
                
                
                
                
                
                
                
                
                
                
                
                
                
                
                
                
                
                
                
                
                
                
                
                
                
                
                
                
                
                
                
                
                prompt_service = PDFPromptService(db, project_id)
                prompt = await prompt_service.get_extraction_prompt(
                    truncated_text, mpn, "Multi-PDF & Multi-MPN Data Extraction.", is_unstructured=False
                )
                if not prompt:
                    error_msg = "No business rule prompt configured for Multi-PDF Extraction"
                    logger.warning(f"{error_msg} for project {project_id}")
                    raise ValueError(error_msg)
                response = await call_llm_with_schema(
                    prompt=prompt,
                    response_model="PDFExtractionResponse",
                    llm_provider='claude',
                    estimated_tokens=4000
                )
                prod_dict = parse_llm_response(response, mpn)
                if prod_dict:
                    product = await upsert_product_from_extraction(
                        db, project_id, mpn, prod_dict, best_pdf['filename']
                    )
                    extracted_count = source.source_metadata.get(
                        "extracted", 0) + 1
                    source.source_metadata["extracted"] = extracted_count
                    flag_modified(source, "source_metadata")
                    if extracted_count >= source.source_metadata.get("total_mpns", 0):
                        source.status = "completed"
                        source.source_metadata["processing_status"] = "completed"
                        source.source_metadata["completed_at"] = now_ist(
                        ).isoformat()

                    db.add(source)
                    await db.commit()
                    await sync_project_status(db, project_id)
                    logger.info(
                        f"    Extracted data for {mpn} (score: {product.completeness_score}%)")
                else:
                    raise ValueError("Could not parse LLM response")
            else:
                raise ValueError(f"No matching PDF found for {mpn}")
        except Exception as e:
            logger.error(
                f" Failed to extract {mpn} from multi-PDF: {e}", exc_info=True)
            await db.rollback()
            stmt = select(Product).join(
                ProjectProductLink, Product.id == ProjectProductLink.product_id  
            ).where(
                Product.product_code == mpn
            )
            result = await db.execute(stmt)
            product = result.scalar_one_or_none()
            if product:
                product.enrichment_status = "failed"
                db.add(product)
            source = await db.get(Source, batch_id)
            if source:
                source.source_metadata["processing_status"] = "failed"
                source.source_metadata["error"] = str(e)
                db.add(source)
            await db.commit()
            await sync_project_status(db, project_id)


@router.post("/extract-pending")
async def extract_pending_pdf(
    data: dict,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session),
):
    try:
        mpn = data.get("mpn")
        project_id = data.get("project_id")
        if not mpn or not project_id:
            raise HTTPException(
                400, "Missing required fields: mpn and project_id")
        stmt = select(Source).where(
            Source.source_type.in_([
                "pdf_pending_extraction",
                "pdf_unstructured_pending",
                "pdf_multi_pending",
                "pdf_blind_pending",
                "pdf_blind_extraction",
            ]),
            Source.project_id == project_id,
        )
        result = await db.execute(stmt)
        sources = result.scalars().all()
        source = None
        for s in sources:
            mpns = s.source_metadata.get("mpns", [])
            single_mpn = s.source_metadata.get("mpn")
            if mpn in mpns or mpn == single_mpn:
                source = s
                break
        if not source:
            raise HTTPException(404, f"PDF source not found for MPN: {mpn}")
        source.status = "processing"
        source.source_metadata["processing_status"] = "processing"
        flag_modified(source, "source_metadata")
        db.add(source)
        await db.execute(
            sa_update(Product)
            .join(
                ProjectProductLink, Product.id == ProjectProductLink.product_id  
            )
            .where(
                ProjectProductLink.project_id == project_id,  
                Product.product_code == mpn,
                Product.enrichment_status.in_(["pending", "failed"]),
            )
            .values(enrichment_status="processing")
        )
        await db.commit()
        if source.source_type == "pdf_multi_pending":
            background_tasks.add_task(
                process_multi_pdf_extraction_for_single_mpn, source.id, mpn, project_id)
        elif source.source_type in ("pdf_blind_pending", "pdf_blind_extraction"):
            background_tasks.add_task(
                process_blind_pdf_extraction, source.id, project_id)
        else:
            background_tasks.add_task(
                process_pdf_extraction_for_product, source.id, mpn, project_id)
        return {"status": "processing", "batch_id": source.id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to start extraction: {e}", exc_info=True)
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
            is_unstructured = source.source_metadata.get(
                "is_unstructured", False)
            if is_unstructured:
                await process_unstructured_pdf_extraction(
                    batch_id, pdf_bytes, mpn, project_id, source.source_url
                )
            else:
                await process_structured_pdf_extraction(
                    batch_id, pdf_bytes, mpn, project_id, source.source_url
                )
            source.source_metadata["extracted"] = True
            source.source_metadata["processing_status"] = "completed"
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


def parse_llm_response(response, mpn: str) -> Optional[Dict]:
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
        return None
    if hasattr(product_info, 'model_dump'):
        return product_info.model_dump()
    elif hasattr(product_info, 'dict'):
        return product_info.dict()
    elif isinstance(product_info, dict):
        return product_info
    else:
        raise ValueError(f"Unexpected product_info type: {type(product_info)}")


async def upsert_product_from_extraction(
    db: AsyncSession,
    project_id: str,
    mpn: str,
    prod_dict: Dict,
    source_url: str
) -> Product:
    spec_count = len(prod_dict.get('specifications', {}))
    completeness_score = min(spec_count * 5, 100)
    enrichment_threshold = getattr(settings, 'enrichment_threshold', 90)
    workflow_stage = 'aggregation'
    needs_enrichment = False
    ready_for_export = True
    routed_to_enrichment_at = None
    enrichment_status = 'completed'
    existing_product = await db.execute(
        select(Product).join(
            ProjectProductLink, Product.id == ProjectProductLink.product_id  
        ).where(
            Product.product_code == mpn
        )
    )
    existing_product = existing_product.scalar_one_or_none()
    if existing_product:
        existing_product.product_name = prod_dict.get(
            'product_name', f"Product {mpn}")
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
        existing_product.source_url = source_url
        existing_product.completeness_score = completeness_score
        db.add(existing_product)
        return existing_product
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
            workflow_stage=workflow_stage,
            needs_enrichment=needs_enrichment,
            ready_for_export=ready_for_export,
            routed_to_enrichment_at=routed_to_enrichment_at,
            enrichment_status=enrichment_status,
            attributes=prod_dict.get('specifications', {}),
            source_url=source_url,
            completeness_score=completeness_score
        )
        db.add(product)
        await db.flush()
        db.add(ProjectProductLink(project_id=project_id, product_id=product.id))
        return product


@router.post('/save-pending-mpns')
async def save_pending_mpns(
    request: FreshAggregationRequest,
    db: AsyncSession = Depends(get_session)
):
    try:
        batch_id = str(uuid.uuid4())
        source = Source(
            id=batch_id,
            source_type='pdf_fresh_pending',
            source_url='fresh_aggregation_pending',
            project_id=request.project_id,
            status='pending',
            source_metadata={
                'mpns': request.mpns,
                'use_case': request.use_case,
                'created_at': now_ist().isoformat(),
                'total': len(request.mpns),
                'extracted': 0,
                'processing_status': 'pending',
                'method': 'web_search'
            }
        )
        db.add(source)
        products_added = 0
        for mpn in request.mpns:
            stmt = select(Product).join(
                ProjectProductLink, Product.id == ProjectProductLink.product_id  
            ).where(
                ProjectProductLink.project_id == request.project_id,  
                Product.product_code == mpn
            )
            result = await db.execute(stmt)
            existing = result.scalar_one_or_none()
            if not existing:
                brand = ""
                product_name = f"{mpn}"
                if hasattr(request, 'detailed_data') and request.detailed_data:
                    for item in request.detailed_data:
                        if item.get('mpn') == mpn:
                            brand = item.get('brand', '')
                            product_name = item.get('product_name', f"{mpn}")
                            break
                product = Product(
                    product_code=mpn,
                    product_name=product_name,
                    brand_name=brand,
                    mpn=mpn,
                    workflow_stage='aggregation',
                    enrichment_status='pending',
                    source_url='web_search_pending',
                    completeness_score=0,
                    attributes={}
                )
                db.add(product)
                await db.flush()
                db.add(ProjectProductLink(
                    project_id=request.project_id, product_id=product.id))
                products_added += 1
        await db.commit()
        logger.info(
            f"Saved {products_added} pending MPNs for project {request.project_id}")
        return {
            'status': 'success',
            'batch_id': batch_id,
            'message': f"Saved {products_added} MPN(s). Extract from Aggregation tab.",
            'saved_count': products_added
        }
    except Exception as e:
        logger.error(f"Failed to save pending MPNs: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(500, str(e))


async def process_unstructured_pdf_extraction(
    batch_id: str,
    pdf_bytes: bytes,
    mpn: str,
    project_id: str,
    filename: str
) -> None:
    async with async_session_factory() as db:
        try:
            logger.info(
                f" Starting unstructured extraction for {mpn} (batch: {batch_id})")
            logger.info(f" Opening PDF: {filename} ({len(pdf_bytes)} bytes)")
            full_text = ""
            with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
                logger.info(
                    f"PDF opened successfully, {len(pdf.pages)} pages")
                for i, page in enumerate(pdf.pages):
                    logger.info(f"Processing page {i+1}/{len(pdf.pages)}...")
                    try:
                        page_text = page.extract_text()
                        if page_text:
                            full_text += page_text + "\n"
                            logger.info(
                                f"   Page {i+1}: extracted {len(page_text)} chars")
                        else:
                            logger.info(f"  Page {i+1}: no text extracted")
                    except Exception as e:
                        logger.warning(f"   Page {i+1} failed: {e}")
                        continue
            truncated_text = full_text[:15000]
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            prompt_service = PDFPromptService(db, project_id)
            prompt = await prompt_service.get_unstructured_extraction_prompt(
                truncated_text, mpn, "Unstructured PDF Extraction (Given MPNs)"
            )
            if not prompt:
                error_msg = "No business rule prompt configured for Unstructured PDF Extraction"
                logger.warning(f"{error_msg} for project {project_id}")

                source = await db.get(Source, batch_id)
                if source:
                    source.status = "failed"
                    source.source_metadata.update({
                        "processing_status": "failed",
                        "error": error_msg,
                        "error_type": "missing_prompt",
                        "failed_at": now_ist().isoformat()
                    })
                    flag_modified(source, "source_metadata")
                    db.add(source)
                    await db.commit()
                    await sync_project_status(db, project_id)

                stmt = select(Product).join(
                    ProjectProductLink, Product.id == ProjectProductLink.product_id  
                ).where(
                    Product.product_code == mpn
                )
                result = await db.execute(stmt)
                product = result.scalar_one_or_none()
                if product:
                    product.enrichment_status = "failed"
                    db.add(product)
                    await db.commit()

                return
            logger.info(f"Extracted text length for {mpn}: {len(full_text)}")
            response = await call_llm_with_schema(
                prompt=prompt,
                response_model="PDFExtractionResponse",
                llm_provider='claude',
                estimated_tokens=4000
            )
            prod_dict = parse_llm_response(response, mpn)
            if not prod_dict:
                raise ValueError(f"Could not extract data for MPN {mpn}")
            product = await upsert_product_from_extraction(db, project_id, mpn, prod_dict, filename)
            source = await db.get(Source, batch_id)
            if source:
                source.status = 'completed'
                source.source_metadata.update({
                    'successful': 1,
                    'failed': 0,
                    'completeness_score': product.completeness_score,
                    'processing_status': 'completed',
                    'completed_at': now_ist().isoformat()
                })
                flag_modified(source, "source_metadata")
                db.add(source)
            await db.commit()
            await sync_project_status(db, project_id)
            logger.info(
                f"Unstructured extraction completed for {mpn} (score {product.completeness_score})")
        except Exception as e:
            logger.error(f"Unstructured extraction failed: {e}", exc_info=True)
            await db.rollback()
            source = await db.get(Source, batch_id)
            if source:
                source.status = 'failed'
                source.source_metadata['error'] = str(e)
                source.source_metadata['processing_status'] = 'failed'
                db.add(source)
                await db.commit()
                await sync_project_status(db, project_id)


@router.post('/multi-pdf-extraction')
async def multi_pdf_extraction(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    mpns: str = Form(...),
    project_id: str = Form(...),
    use_case: str = Form(...),
    db: AsyncSession = Depends(get_session)
):

    try:
        mpn_list = [m.strip() for m in mpns.split(',') if m.strip()]
        if len(mpn_list) == 0:
            raise HTTPException(400, "At least one MPN is required")
        if len(files) == 0:
            raise HTTPException(400, "At least one PDF file is required")
        if len(files) > 20:
            raise HTTPException(400, "Maximum 20 PDF files allowed")
        if len(mpn_list) > 50:
            raise HTTPException(400, "Maximum 50 MPNs allowed")
        batch_id = str(uuid.uuid4())
        pdf_documents = []
        total_size = 0
        MAX_TOTAL_SIZE = 100 * 1024 * 1024
        for file in files:
            if not file.filename.lower().endswith('.pdf'):
                raise HTTPException(
                    400, f"File '{file.filename}' is not a PDF")
            pdf_bytes = await file.read()
            total_size += len(pdf_bytes)
            if total_size > MAX_TOTAL_SIZE:
                raise HTTPException(
                    400, f"Total file size exceeds {MAX_TOTAL_SIZE // (1024*1024)} MB")
            pdf_documents.append({
                'filename': file.filename,
                'content': pdf_bytes,
                'size': len(pdf_bytes)
            })
        source = Source(
            id=batch_id,
            source_type="pdf_multi_pending",
            source_url=f"multi_pdf_{len(pdf_documents)}_files_{len(mpn_list)}_mpns",
            project_id=project_id,
            status="pending",
            content_data=None,
            source_metadata={
                "mpns": mpn_list,
                "use_case": use_case,
                "pdf_files": [{'filename': p['filename'], 'size': p['size']} for p in pdf_documents],
                "total_pdfs": len(pdf_documents),
                "total_mpns": len(mpn_list),
                "total_size": total_size,
                "created_at": now_ist().isoformat(),
                "extracted": 0,
                "processing_status": "pending",
                "extraction_type": "multi",
                "is_unstructured": False
            }
        )
        db.add(source)
        source.content_data = pickle.dumps(pdf_documents)
        products_added = 0
        for mpn in mpn_list:
            stmt = select(Product).join(
                ProjectProductLink, Product.id == ProjectProductLink.product_id  
            ).where(
                Product.product_code == mpn
            )
            result = await db.execute(stmt)
            existing = result.scalar_one_or_none()
            if not existing:
                product = Product(
                    product_code=mpn,
                    product_name=f"{mpn}",
                    mpn=mpn,
                    workflow_stage="aggregation",
                    enrichment_status="pending",
                    source_url=f"multi_pdf_batch_{batch_id[:8]}",
                    completeness_score=0,
                    attributes={}
                )
                db.add(product)
                await db.flush()
                db.add(ProjectProductLink(
                    project_id=project_id, product_id=product.id))
                products_added += 1
        await db.commit()
        return {
            "status": "success",
            "batch_id": batch_id,
            "message": f"Saved {len(mpn_list)} MPN(s) and {len(pdf_documents)} PDF(s). Extract from Aggregation tab.",
            "mpns_count": len(mpn_list),
            "pdfs_count": len(pdf_documents),
            "products_created": products_added
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to save multi-PDF extraction: {e}", exc_info=True)
        raise HTTPException(500, str(e))


async def process_multi_pdf_extraction(
    batch_id: str,
    pdf_documents: List[Dict],
    mpns: List[str],
    project_id: str
) -> None:
    async with async_session_factory() as db:
        try:
            logger.info(
                f" Starting multi-PDF extraction: {len(mpns)} MPNs, {len(pdf_documents)} PDFs")
            source = await db.get(Source, batch_id)
            if source:
                source.status = "processing"
                source.source_metadata["processing_status"] = "processing"
                flag_modified(source, "source_metadata")
                db.add(source)
                await db.commit()
            pdf_texts = []
            for pdf_doc in pdf_documents:
                try:
                    full_text = extract_pdf_text(
                        pdf_doc['content'], max_pages=60)
                    pdf_texts.append({
                        'filename': pdf_doc['filename'],
                        'text': full_text,
                        'length': len(full_text)
                    })
                    logger.info(
                        f"📄 Extracted {len(full_text)} chars from {pdf_doc['filename']}")
                except Exception as e:
                    logger.error(
                        f"Failed to extract {pdf_doc['filename']}: {e}")
                    pdf_texts.append({
                        'filename': pdf_doc['filename'],
                        'text': "",
                        'length': 0,
                        'error': str(e)
                    })
            successful = 0
            failed = 0
            results = []
            for mpn in mpns:
                try:
                    logger.info(f"🔍 Processing MPN: {mpn}")
                    stmt = select(Product).join(
                        ProjectProductLink, Product.id == ProjectProductLink.product_id  
                    ).where(
                        Product.product_code == mpn
                    )
                    result = await db.execute(stmt)
                    product = result.scalar_one_or_none()
                    if not product:
                        logger.warning(f"Product not found for MPN: {mpn}")
                        failed += 1
                        continue
                    product.enrichment_status = "processing"
                    db.add(product)
                    await db.commit()
                    best_pdf = None
                    best_score = 0
                    for pdf_text in pdf_texts:
                        if not pdf_text["text"]:
                            continue
                        score = score_pdf_text_for_mpn(
                            pdf_text["text"], pdf_text["filename"], mpn)
                        logger.info(
                            f"Match score for {pdf_text['filename']}: {score}")
                        if score > best_score:
                            best_score = score
                            best_pdf = pdf_text
                    if best_pdf and best_score > 0:
                        logger.info(
                            f"Best match: {best_pdf['filename']} (score: {best_score})")
                        truncated_text = slice_text_around_mpn(
                            best_pdf["text"], mpn, window=15000, back=6000)
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                        prompt_service = PDFPromptService(db, project_id)
                        prompt = await prompt_service.get_extraction_prompt(
                            truncated_text, mpn, "Multi-PDF & Multi-MPN Data Extraction.", is_unstructured=False
                        )

                        if not prompt:
                            error_msg = "No business rule prompt configured for Multi-PDF Extraction"
                            logger.warning(
                                f"{error_msg} for project {project_id}")
                            raise ValueError(error_msg)
                        response = await call_llm_with_schema(
                            prompt=prompt,
                            response_model="PDFExtractionResponse",
                            llm_provider='claude',
                            estimated_tokens=4000
                        )
                        prod_dict = parse_llm_response(response, mpn)
                        if prod_dict:
                            product = await upsert_product_from_extraction(
                                db, project_id, mpn, prod_dict, best_pdf['filename']
                            )
                            successful += 1
                            source = await db.get(Source, batch_id)
                            if source:
                                extracted_count = source.source_metadata.get(
                                    "extracted", 0) + 1
                                source.source_metadata["extracted"] = extracted_count
                                flag_modified(source, "source_metadata")
                                db.add(source)
                                await db.commit()
                            results.append({
                                'mpn': mpn,
                                'status': 'success',
                                'pdf': best_pdf['filename'],
                                'score': best_score,
                                'completeness': product.completeness_score
                            })
                            logger.info(
                                f"    Extracted data for {mpn} (score: {product.completeness_score}%)")
                        else:
                            raise ValueError("Could not extract data")
                    else:
                        logger.warning(f"    No matching PDF found for {mpn}")
                        product.enrichment_status = "failed"
                        db.add(product)
                        failed += 1
                        results.append({
                            'mpn': mpn,
                            'status': 'failed',
                            'reason': 'No matching PDF found'
                        })
                except Exception as e:
                    logger.error(f"    Failed to process {mpn}: {e}")
                    failed += 1
                    results.append({
                        'mpn': mpn,
                        'status': 'failed',
                        'reason': str(e)
                    })
                    stmt = select(Product).join(
                        ProjectProductLink, Product.id == ProjectProductLink.product_id  
                    ).where(
                        Product.product_code == mpn
                    )
                    result = await db.execute(stmt)
                    product = result.scalar_one_or_none()
                    if product:
                        product.enrichment_status = "failed"
                        db.add(product)
            source = await db.get(Source, batch_id)
            if source:
                source.status = "completed" if successful > 0 else "failed"
                source.source_metadata.update({
                    "processing_status": "completed" if successful > 0 else "failed",
                    "successful": successful,
                    "failed": failed,
                    "results": results,
                    "completed_at": now_ist().isoformat()
                })
                flag_modified(source, "source_metadata")
                db.add(source)
            await db.commit()
            await sync_project_status(db, project_id)
            logger.info(
                f" Multi-PDF extraction completed: {successful} successful, {failed} failed")
        except Exception as e:
            logger.error(f" Multi-PDF extraction failed: {e}", exc_info=True)
            await db.rollback()
            source = await db.get(Source, batch_id)
            if source:
                source.status = "failed"
                source.source_metadata["error"] = str(e)
                source.source_metadata["processing_status"] = "failed"
                db.add(source)
                await db.commit()
                await sync_project_status(db, project_id)


@router.post('/process-excel-mpns/{batch_id}')
async def process_excel_mpns(
    batch_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session)
):
    try:
        source = await db.get(Source, batch_id)
        if not source:
            raise HTTPException(404, "Batch not found")
        if source.status == "processing":
            raise HTTPException(400, "Batch is already processing")
        mpns = source.source_metadata.get('mpns', [])
        use_case = source.source_metadata.get('use_case')
        project_id = source.project_id
        detailed_data = source.source_metadata.get('detailed_data', [])
        if not mpns:
            raise HTTPException(400, "No MPNs to process")
        source.status = "processing"
        source.source_metadata['processing_status'] = 'processing'
        source.source_metadata['started_at'] = now_ist().isoformat()
        db.add(source)
        await db.commit()
        if 'Fresh PDF Aggregation' in use_case:
            background_tasks.add_task(
                process_fresh_pdf_aggregation,
                batch_id, mpns, project_id, detailed_data
            )
        else:
            request = FreshAggregationRequest(
                mpns=mpns,
                project_id=project_id,
                use_case=use_case
            )
            background_tasks.add_task(
                _create_pending_products_with_context,
                batch_id, mpns, project_id, detailed_data
            )
        return {
            'status': 'processing',
            'batch_id': batch_id,
            'message': f"Processing {len(mpns)} MPN(s)",
            'total': len(mpns)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to process Excel MPNs: {e}", exc_info=True)
        raise HTTPException(500, str(e))


@router.get('/download-mpn-template')
async def download_mpn_template():
    """
    Download Excel template for MPN upload.
    """
    try:
        template_bytes = ExcelMPNService.generate_template()
        return StreamingResponse(
            BytesIO(template_bytes),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": "attachment; filename=mpn_upload_template.xlsx"
            }
        )
    except Exception as e:
        logger.error(f"Failed to generate template: {e}")
        raise HTTPException(500, "Failed to generate template")


@router.post('/upload-mpns-excel')
async def upload_mpns_from_excel(
    file: UploadFile = File(...),
    project_id: str = Form(...),
    use_case: str = Form(...),
    sheet_name: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_session)
):
    """Upload Excel/CSV file containing MPNs."""
    try:
        file_bytes = await file.read()
        is_valid, message = ExcelMPNService.validate_file(
            file_bytes, file.filename)
        if not is_valid:
            raise HTTPException(400, message)
        result = ExcelMPNService.parse_mpns_from_excel(
            file_bytes, file.filename, sheet_name)
        if result['metadata']['errors'] and not result['mpns']:
            error_msg = "; ".join(result['metadata']['errors'][:3])
            raise HTTPException(400, f"Failed to parse file: {error_msg}")
        batch_id = str(uuid.uuid4())
        source = Source(
            id=batch_id,
            source_type="excel_mpn_upload",
            source_url=file.filename,
            project_id=project_id,
            status="pending",
            source_metadata={
                'use_case': use_case,
                'total_mpns': len(result['mpns']),
                'valid_mpns': result['metadata']['valid_mpns'],
                'invalid_mpns': result['metadata']['invalid_mpns'],
                'duplicates_removed': result['metadata']['duplicates_removed'],
                'mpns': result['mpns'],
                'detailed_data': result.get('detailed_data', []),
                'parse_metadata': result['metadata'],
                'created_at': now_ist().isoformat(),
                'processing_status': 'pending'
            }
        )
        db.add(source)
        await db.commit()
        return {
            'status': 'success',
            'batch_id': batch_id,
            'filename': file.filename,
            'total_mpns': len(result['mpns']),
            'valid_mpns': result['metadata']['valid_mpns'],
            'invalid_mpns': result['metadata']['invalid_mpns'],
            'duplicates_removed': result['metadata']['duplicates_removed'],
            'warnings': result['metadata']['warnings'],
            'mpns_preview': result['mpns'][:10],
            'has_more': len(result['mpns']) > 10,
            'mpn_column_used': result['metadata']['mpn_column'],
            'brand_column_found': result['metadata']['brand_column'],
            'product_name_column_found': result['metadata']['product_name_column']
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to upload MPNs Excel: {e}", exc_info=True)
        raise HTTPException(500, f"Internal server error: {str(e)}")


async def _create_pending_products_with_context(
    batch_id: str,
    mpns: List[str],
    project_id: str,
    detailed_data: List[Dict] = None
):
    async with async_session_factory() as db:
        try:
            context_map = {}
            if detailed_data:
                for item in detailed_data:
                    context_map[item.get('mpn', '')] = {
                        'brand': item.get('brand', ''),
                        'product_name': item.get('product_name', '')
                    }
            products_added = 0
            for mpn in mpns:
                stmt = select(Product).join(
                    ProjectProductLink, Product.id == ProjectProductLink.product_id  
                ).where(
                    Product.product_code == mpn
                )
                result = await db.execute(stmt)
                existing = result.scalar_one_or_none()
                if not existing:
                    ctx = context_map.get(mpn, {})
                    product = Product(
                        product_code=mpn,
                        product_name=ctx.get('product_name', f"{mpn}"),
                        brand_name=ctx.get('brand', ''),
                        mpn=mpn,
                        workflow_stage='aggregation',
                        enrichment_status='pending',
                        source_url='excel_upload_pending',
                        completeness_score=0,
                        attributes={}
                    )
                    db.add(product)
                    await db.flush()
                    db.add(ProjectProductLink(
                        project_id=project_id, product_id=product.id))
                    products_added += 1
            await db.commit()
            source = await db.get(Source, batch_id)
            if source:
                source.status = 'completed'
                source.source_metadata['processing_status'] = 'completed'
                source.source_metadata['products_created'] = products_added
                flag_modified(source, "source_metadata")
                db.add(source)
                await db.commit()
            logger.info(
                f"Created {products_added} pending products from Excel upload")
        except Exception as e:
            logger.error(
                f"Failed to create pending products: {e}", exc_info=True)
            await db.rollback()
            source = await db.get(Source, batch_id)
            if source:
                source.status = 'failed'
                source.source_metadata['error'] = str(e)
                source.source_metadata['processing_status'] = 'failed'
                flag_modified(source, "source_metadata")
                db.add(source)
                await db.commit()


@router.post('/parse-mpns-excel')
async def parse_mpns_from_excel(
    file: UploadFile = File(...),
    sheet_name: Optional[str] = Form(None),
):
    try:
        file_bytes = await file.read()
        is_valid, message = ExcelMPNService.validate_file(
            file_bytes, file.filename)
        if not is_valid:
            raise HTTPException(400, message)
        result = ExcelMPNService.parse_mpns_from_excel(
            file_bytes,
            file.filename,
            sheet_name
        )
        return {
            'status': 'success',
            'valid_mpns': len(result['mpns']),
            'mpns': result['mpns'],
            'duplicates_removed': result['metadata']['duplicates_removed'],
            'invalid_mpns': result['metadata']['invalid_mpns']
        }
    except Exception as e:
        logger.error(f"Failed to parse Excel: {e}")
        raise HTTPException(500, str(e))
