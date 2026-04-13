import os
from io import BytesIO
import pdfplumber
import json
from app.core.config import settings
from sqlmodel import select
from sqlalchemy import func, case
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm.attributes import flag_modified
from datetime import datetime
import aiohttp
from app.aggregation.services.pdf_service import PDFExtractionService
from app.core.database import get_session
from app.llm import call_llm_with_schema
from app.models.pipeline import Source
from app.models.product import Product
from app.models.project import Project
from app.schemas.pdf_extraction import FreshAggregationRequest, PDFExtractionResponse
import uuid
import logging
from typing import List, Optional, Dict
from app.search.searxng_service import SearXNGSearchService
from app.core.database import async_session_factory
logger = logging.getLogger('pdf extraction')
router = APIRouter()


# async def sync_project_status(db, project_id: str) -> None:
#     project = await db.get(Project, project_id)
#     if not project:
#         return
#     stmt = select(
#         func.count(Product.id),
#         func.sum(case((Product.enrichment_status == "completed", 1), else_=0)),
#         func.sum(case((Product.enrichment_status == "failed", 1), else_=0)),
#         func.sum(case((Product.enrichment_status == "processing", 1), else_=0)),
#         func.sum(case((Product.enrichment_status == "pending", 1), else_=0)),
#     ).where(Product.project_id == project_id)
#     result = await db.execute(stmt)
#     row = result.one()
#     total = row[0] or 0
#     completed = row[1] or 0
#     failed = row[2] or 0
#     processing = row[3] or 0
#     pending = row[4] or 0
#     if total == 0:
#         project.status = "draft"
#     elif processing > 0:
#         project.status = "processing"
#     elif completed == total:
#         project.status = "completed"
#     elif failed == total:
#         project.status = "failed"
#     elif completed > 0:
#         project.status = "partially_completed"
#     else:
#         project.status = "draft"
#     db.add(project)
#     await db.commit()
async def sync_project_status(db, project_id: str) -> None:
    project = await db.get(Project, project_id)
    if not project:
        return
    
    # Count products
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
    
    # ✅ Check if source failed with no products
    source_stmt = select(Source).where(
        Source.project_id == project_id,
        Source.status == "failed"
    )
    source_result = await db.execute(source_stmt)
    failed_source = source_result.scalars().first()
    
    if total == 0:
        if failed_source:
            project.status = "failed"  # ✅ Source failed, no products
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
    
@router.post('/fresh-aggregation')
async def fresh_aggregation(
    request: FreshAggregationRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session)
):
    try:
        stmt = select(Source).where(
            Source.project_id == request.project_id,
            Source.source_type == 'pdf_fresh_pending',
            Source.status == 'pending'
        )
        result = await db.execute(stmt)
        existing_source = result.scalar_one_or_none()
        if existing_source:
            batch_id = existing_source.id
            existing_source.status = 'processing'
            existing_source.source_metadata['processing_status'] = 'processing'
            existing_source.source_metadata['started_at'] = datetime.utcnow(
            ).isoformat()
            db.add(existing_source)
        else:
            batch_id = str(uuid.uuid4())
            source = Source(
                id=batch_id,
                source_type="pdf_fresh_aggregation",
                source_url='web_enrichment',
                project_id=request.project_id,
                status='processing',
                source_metadata={
                    'mpns': request.mpns,
                    'use_case': request.use_case,
                    'started_at': datetime.utcnow().isoformat(),
                    'total': len(request.mpns),
                    'successful': 0,
                    'failed': 0,
                    'processing_status': 'processing'
                }
            )
            db.add(source)
        await db.commit()
        background_tasks.add_task(
            process_fresh_pdf_aggregation,
            batch_id,
            request.mpns,
            request.project_id
        )
        return {
            'status': 'processing',
            'batch_id': batch_id,
            'message': f"Processing {len(request.mpns)} MPN(s)"
        }
    except Exception as e:
        logger.error(f"Failed to start fresh aggregation: {e}")
        raise HTTPException(500, str(e))


async def search_pdfs_url(mpn: str, brand: str) -> List[str]:
    try:
        logger.info(f"🔍 [1/4] Starting PDF search for MPN: {mpn}")
        searxng = SearXNGSearchService(
            base_url="http://searxng:8080", max_results=20)
        search_queries = [
            f"{mpn} datasheet pdf",
            f"{mpn} specification sheet pdf",
            f"{mpn} data sheet pdf",
            f"{brand} {mpn} product pdf" if brand else f"{mpn} product pdf"
        ]
        logger.info(f"📋 Search queries: {search_queries}")
        pdf_urls = []
        for query in search_queries:
            logger.info(f"🔎 Executing search: {query}")
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

        import pickle
        source = Source(
            id=batch_id,
            source_type="pdf_blind_extraction",
            source_url=f"blind_pdf_{len(pdf_documents)}_files",
            project_id=project_id,
            status="processing",  # Start as processing, not pending
            content_data=pickle.dumps(pdf_documents),
            source_metadata={
                "use_case": use_case,
                "pdf_files": [{'filename': p['filename'], 'size': p['size']} for p in pdf_documents],
                "total_pdfs": len(pdf_documents),
                "created_at": datetime.utcnow().isoformat(),
                "processing_status": "processing",
                "extraction_type": "blind",
                 "product_hint": product_hint.strip(), 
            }
        )
        db.add(source)
        await db.commit()

        # Fire and forget - process immediately
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


async def extract_product_with_claude(mpn: str, pdf_text: str, source_url: str) -> Optional[Dict]:
    try:
        prompt = f"""
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
        schema = {
            "type": 'object',
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
            import pickle
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
                        product_hint
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
                                product_info
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
            source = await db.get(Source, batch_id)  # 
            if source:
                source.status = "completed" if successful > 0 else "failed"
                source.source_metadata.update({
                    "processing_status": "completed" if successful > 0 else "failed",
                    "successful": successful,
                    "failed": failed,
                    "extracted_products": all_extracted_products,
                    "completed_at": datetime.utcnow().isoformat()
                })
                flag_modified(source, "source_metadata")  #
                db.add(source)
                await db.commit()
                await db.refresh(source)
                await sync_project_status(db, project_id)
                logger.info(f"✅ Source {batch_id} updated to {source.status}")
            await sync_project_status(db, project_id)

            logger.info(f" Blind extraction completed: {successful} products extracted")

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
async def identify_products_in_pdf(pdf_text: str, filename: str, product_hint: str) -> List[Dict]:
    # ✅ STRICT PRE-CHECK: If hint is not in the document text, skip Claude entirely
    text_lower = pdf_text.lower()
    hint_lower = product_hint.lower()
    
    # Check if hint appears anywhere in the document
    if hint_lower not in text_lower:
        logger.info(f"❌ Hint '{product_hint}' NOT found in document '{filename}', skipping extraction")
        return []
    
    logger.info(f"✅ Hint '{product_hint}' found in document, proceeding with Claude extraction")
    
    prompt = f"""
You are a product identification expert. Your task is to find products that match the following description.

PRODUCT HINT: "{product_hint}"

Document: {filename}
Content:
{pdf_text[:15000]}

CRITICAL INSTRUCTIONS:
1. The hint "{product_hint}" appears in this document.
2. Find the product that contains or is associated with this hint.
3. If the hint appears in a product name, model number, SKU, or description, extract that product.
4. Return ONLY the product that contains the hint.

For the matching product, provide:
- title: The exact product name/title
- context: Brief description
- confidence: 0.0-1.0

Format:
[{{"title": "Product Name", "context": "Contains hint...", "confidence": 0.95}}]
"""

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
                validated.append({"title": item, "context": f"Found in {filename}", "confidence": 0.7})
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

async def extract_blind_product_details(pdf_text: str, product_info: Dict) -> Optional[Dict]:
    """Extract detailed specifications for an identified product."""

    title = product_info.get('title', 'Unknown Product')
    context = product_info.get('context', '')

    prompt = f"""
You are a product data extraction expert. Extract detailed information for this product:

Product: {title}
Context from document: {context}

Document content:
{pdf_text[:12000]}

Extract the following fields. If not found, leave empty:
- product_name: Full product name
- brand_name: Manufacturer/brand name
- sku: Stock Keeping Unit (if available)
- taxonomy: Product category (e.g., "Electronics > Computers > Laptops")
- description: Product description/summary
- image_url: Main product image URL (if referenced)
- specifications: Key technical specifications as key-value pairs

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
    import re
    slug = re.sub(r'[^a-zA-Z0-9]', '-', title.lower())
    slug = re.sub(r'-+', '-', slug).strip('-')
    timestamp = datetime.utcnow().strftime('%Y%m%d%H%M%S')
    return f"{slug[:30]}-{timestamp[-6:]}"


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
                        logger.info(
                            f"Processing MPN {idx+1}/{len(mpns)}: {mpn}")
                        stmt = select(Product).where(
                            Product.project_id == project_id,
                            Product.product_code == mpn
                        )
                        result = await db.execute(stmt)
                        existing_product = result.scalar_one_or_none()
                        pdf_urls = await search_pdfs_url(mpn, brand='')
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
                                    project_id=project_id,
                                    workflow_stage='aggregation',
                                    enrichment_status='failed',
                                    source_url='web_search_failed',
                                    completeness_score=0
                                )
                                db.add(product)
                            continue
                        product_data = None
                        for pdf_url in pdf_urls:
                            pdf_text = await download_and_extract_pdf(pdf_url)
                            if pdf_text:
                                product_data = await extract_product_with_claude(mpn, pdf_text, pdf_url)
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
                                    project_id=project_id,
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
                                    project_id=project_id,
                                    workflow_stage='aggregation',
                                    enrichment_status='failed',
                                    source_url='web_search_failed',
                                    completeness_score=0
                                )
                                db.add(product)
                    except Exception as e:
                        logger.error(f"Error processing MPN {mpn}: {e}")
                        failed_mpns.append(mpn)
                        stmt = select(Product).where(
                            Product.project_id == project_id,
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
                                project_id=project_id,
                                workflow_stage='aggregation',
                                enrichment_status='failed',
                                source_url='web_search_error',
                                completeness_score=0
                            )
                            db.add(product)
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
                            await sync_project_status(db, project_id)
                source = await db.get(Source, batch_id)
                if source:
                    source.status = 'completed' if successful > 0 else 'failed'
                    source.source_metadata.update({
                        'completed_at': datetime.utcnow().isoformat(),
                        'products': products,
                        'failed_mpns': failed_mpns,
                        'routed_to_enrichment': routed_to_enrichment_count,
                        'processing_status': 'completed' if successful > 0 else 'failed',
                        'ready_for_export': ready_for_export_count
                    })
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
                'started_at': datetime.utcnow().isoformat(),
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


# async def process_structured_pdf_extraction(
#     batch_id: str,
#     pdf_bytes: bytes,
#     mpn: str,
#     project_id: str,
#     filename: str
# ) -> None:
#     async with async_session_factory() as db:
#         try:
#             full_text = ""
#             tables = []
#             with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
#                 for page in pdf.pages:
#                     page_text = page.extract_text()
#                     if page_text:
#                         full_text += page_text + "\n"
#                     page_tables = page.extract_tables()
#                     for tbl in page_tables:
#                         if tbl and len(tbl) > 1:
#                             headers = [
#                                 str(h).strip() if h else "" for h in tbl[0]]
#                             for row in tbl[1:]:
#                                 if not any(cell for cell in row):
#                                     continue
#                                 row_dict = {}
#                                 for idx, cell in enumerate(row):
#                                     if idx < len(headers):
#                                         key = headers[idx]
#                                         val = str(cell).strip() if cell else ""
#                                         if key and val:
#                                             row_dict[key] = val
#                                 if row_dict:
#                                     tables.append(row_dict)
#             truncated_text = full_text[:15000]
#             truncated_tables = json.dumps(tables, indent=2)[:5000]
#             prompt = f"""
# You are a product data extraction expert. Extract product information for MPN "{mpn}" from the document below.
# Use only the content provided; do not search externally.
# Document content (text):
# {truncated_text}
# Table data (extracted from the document):
# {truncated_tables}
# Extract the following fields if present. If a field is missing, leave it empty:
# - product_name
# - brand_name
# - sku
# - taxonomy
# - description
# - image_url
# - specifications
# Return ONLY a single valid JSON object in this format:
# {{
#   "product_name": "",
#   "brand_name": "",
#   "sku": "",
#   "taxonomy": "",
#   "description": "",
#   "image_url": "",
#   "specifications": {{}}
# }}
# """
#             from pydantic import BaseModel, Field
#             from typing import Dict
#             logger.info(f"Extracted text length for {mpn}: {len(full_text)}")
#             logger.info(
#                 f"Extracted text preview for {mpn}: {full_text[:1000]}")
#             logger.info(f"Extracted tables count for {mpn}: {len(tables)}")
#             response = await call_llm_with_schema(
#                 prompt=prompt,
#                 response_model="PDFExtractionResponse",
#                 llm_provider='claude',
#                 estimated_tokens=4000
#             )
#             product_info = None
#             resp_dict = None
#             if response:
#                 if hasattr(response, "model_dump"):
#                     resp_dict = response.model_dump()
#                 elif hasattr(response, "dict"):
#                     resp_dict = response.dict()
#                 elif isinstance(response, dict):
#                     resp_dict = response
#             logger.info(f"LLM parsed response for {mpn}: {resp_dict}")
#             if resp_dict:
#                 if isinstance(resp_dict.get("data"), dict):
#                     product_info = resp_dict["data"].get(mpn)
#                 elif mpn in resp_dict:
#                     product_info = resp_dict[mpn]
#                 elif "product_name" in resp_dict or "specifications" in resp_dict:
#                     product_info = resp_dict
#             if not product_info:
#                 logger.warning(
#                     f"MPN {mpn} not found or could not be extracted from PDF")
#                 source = await db.get(Source, batch_id)
#                 if source:
#                     source.status = 'failed'
#                     source.source_metadata['error'] = f"MPN {mpn} not found in PDF"
#                     source.source_metadata['failed'] = 1
#                     source.source_metadata['completed_at'] = datetime.utcnow(
#                     ).isoformat()
#                     source.source_metadata['processing_status'] = 'failed'
#                     db.add(source)
#                     await db.commit()
#                     await sync_project_status(db, project_id)
#                 return
#             if hasattr(product_info, 'model_dump'):
#                 prod_dict = product_info.model_dump()
#             elif hasattr(product_info, 'dict'):
#                 prod_dict = product_info.dict()
#             elif isinstance(product_info, dict):
#                 prod_dict = product_info
#             else:
#                 raise ValueError(
#                     f"Unexpected product_info type: {type(product_info)}")
#             spec_count = len(prod_dict.get('specifications', {}))
#             completeness_score = min(spec_count * 5, 100)
#             enrichment_threshold = getattr(
#                 settings, 'enrichment_threshold', 90)
#             if completeness_score < enrichment_threshold:
#                 workflow_stage = 'enrichment'
#                 needs_enrichment = True
#                 ready_for_export = False
#                 routed_to_enrichment_at = datetime.utcnow()
#                 enrichment_status = 'pending'
#             else:
#                 workflow_stage = 'aggregation'
#                 needs_enrichment = False
#                 ready_for_export = True
#                 routed_to_enrichment_at = None
#                 enrichment_status = 'completed'
#             existing_product = await db.execute(
#                 select(Product).where(
#                     Product.project_id == project_id,
#                     Product.product_code == mpn
#                 )
#             )
#             existing_product = existing_product.scalar_one_or_none()
#             if existing_product:
#                 existing_product.product_name = prod_dict.get(
#                     'product_name', f"Product {mpn}")
#                 existing_product.brand_name = prod_dict.get('brand_name', "")
#                 existing_product.sku = prod_dict.get('sku', "")
#                 existing_product.taxonomy = prod_dict.get('taxonomy', '')
#                 existing_product.description = prod_dict.get('description', '')
#                 existing_product.image_url_1 = prod_dict.get('image_url', '')
#                 existing_product.workflow_stage = workflow_stage
#                 existing_product.needs_enrichment = needs_enrichment
#                 existing_product.ready_for_export = ready_for_export
#                 existing_product.routed_to_enrichment_at = routed_to_enrichment_at
#                 existing_product.enrichment_status = enrichment_status
#                 existing_product.attributes = prod_dict.get(
#                     'specifications', {})
#                 existing_product.source_url = filename
#                 existing_product.completeness_score = completeness_score
#                 db.add(existing_product)
#             else:
#                 product = Product(
#                     product_code=mpn,
#                     product_name=prod_dict.get(
#                         'product_name', f"Product {mpn}"),
#                     brand_name=prod_dict.get('brand_name', ""),
#                     mpn=mpn,
#                     sku=prod_dict.get('sku', ""),
#                     taxonomy=prod_dict.get('taxonomy', ''),
#                     description=prod_dict.get('description', ''),
#                     image_url_1=prod_dict.get('image_url', ''),
#                     project_id=project_id,
#                     workflow_stage=workflow_stage,
#                     needs_enrichment=needs_enrichment,
#                     ready_for_export=ready_for_export,
#                     routed_to_enrichment_at=routed_to_enrichment_at,
#                     enrichment_status=enrichment_status,
#                     attributes=prod_dict.get('specifications', {}),
#                     source_url=filename,
#                     completeness_score=completeness_score
#                 )
#                 db.add(product)
#             await db.flush()
#             source = await db.get(Source, batch_id)
#             if source:
#                 source.status = 'completed'
#                 source.source_metadata.update({
#                     'successful': 1,
#                     'failed': 0,
#                     'product': prod_dict,
#                     'mpn': mpn,
#                     'completeness_score': completeness_score,
#                     'workflow_stage': workflow_stage,
#                     'completed_at': datetime.utcnow().isoformat(),
#                     'processing_status': 'completed'
#                 })
#                 db.add(source)
#             await db.commit()
#             await sync_project_status(db, project_id)
#             logger.info(
#                 f"Structured extraction completed for MPN {mpn} (score {completeness_score}, stage {workflow_stage})")
#         except Exception as e:
#             logger.error(
#                 f"Structured PDF extraction failed for batch {batch_id}: {e}", exc_info=True)
#             await db.rollback()
#             source = await db.get(Source, batch_id)
#             if source:
#                 source.status = 'failed'
#                 source.source_metadata['error'] = str(e)
#                 source.source_metadata['processing_status'] = 'failed'
#                 db.add(source)
#                 await db.commit()
#                 await sync_project_status(db, project_id)
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
            prompt = f"""
You are a product data extraction expert. Extract product information for MPN "{mpn}" from the document below.
Document content (text):
{truncated_text}
Table data:
{truncated_tables}
Extract: product_name, brand_name, sku, taxonomy, description, image_url, specifications
Return ONLY valid JSON.
"""
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
                    'completed_at': datetime.utcnow().isoformat()
                })
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
        stmt = select(Product).where(
            Product.project_id == project_id,
            Product.product_code == mpn
        )
        result = await db.execute(stmt)
        existing = result.scalar_one_or_none()
        if not existing:
            product = Product(
                product_code=mpn,
                product_name=f"{mpn}",
                mpn=mpn,
                project_id=project_id,
                workflow_stage="aggregation",
                enrichment_status="pending",
                source_url=file.filename,
                completeness_score=0
            )
            db.add(product)
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
            import pickle
            source = await db.get(Source, batch_id)
            if not source:
                logger.error(f"Source not found: {batch_id}")
                return

            # Get the stored PDFs
            pdf_documents = pickle.loads(
                source.content_data) if source.content_data else []

            if not pdf_documents:
                raise ValueError("No PDFs found in source")

            logger.info(
                f"📄 Processing MPN {mpn} from multi-PDF source with {len(pdf_documents)} PDFs")

            # Extract text from all PDFs
            pdf_texts = []
            for pdf_doc in pdf_documents:
                try:
                    full_text = ""
                    with pdfplumber.open(BytesIO(pdf_doc['content'])) as pdf:
                        for page in pdf.pages[:15]:
                            page_text = page.extract_text()
                            if page_text:
                                full_text += page_text + "\n"

                    pdf_texts.append({
                        'filename': pdf_doc['filename'],
                        'text': full_text[:20000],
                        'length': len(full_text)
                    })
                    logger.info(
                        f"   Extracted {len(full_text)} chars from {pdf_doc['filename']}")
                except Exception as e:
                    logger.error(
                        f"   Failed to extract {pdf_doc['filename']}: {e}")

            # Find best matching PDF
            best_pdf = None
            best_score = 0

            for pdf_text in pdf_texts:
                if not pdf_text['text']:
                    continue

                score = 0
                text_lower = pdf_text['text'].lower()
                mpn_lower = mpn.lower()

                if mpn_lower in text_lower:
                    score += 100

                mpn_parts = mpn_lower.replace(
                    '-', ' ').replace('_', ' ').split()
                for part in mpn_parts:
                    if len(part) > 2 and part in text_lower:
                        score += 20

                if mpn_lower in pdf_text['filename'].lower():
                    score += 50

                if score > best_score:
                    best_score = score
                    best_pdf = pdf_text

            if best_pdf and best_score > 0:
                logger.info(
                    f"    Best match: {best_pdf['filename']} (score: {best_score})")

                truncated_text = best_pdf['text'][:15000]
                prompt = f"""
You are a product data extraction expert. Extract product information for MPN "{mpn}" from the document below.
The document is a product specification/catalog PDF. Extract whatever product specifications you can find.

Document content:
{truncated_text}

Look for:
- Product name/title
- Brand/manufacturer name
- Technical specifications (dimensions, weight, materials, features)
- Product category/taxonomy

Extract the following fields if present. If missing, leave empty:
- product_name
- brand_name
- sku
- taxonomy
- description
- image_url
- specifications (as key-value pairs)

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

                    # Update source metadata
                    extracted_count = source.source_metadata.get(
                        "extracted", 0) + 1
                    source.source_metadata["extracted"] = extracted_count

                    if extracted_count >= source.source_metadata.get("total_mpns", 0):
                        source.status = "completed"
                        source.source_metadata["processing_status"] = "completed"

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

            # Update product as failed
            stmt = select(Product).where(
                Product.project_id == project_id,
                Product.product_code == mpn
            )
            result = await db.execute(stmt)
            product = result.scalar_one_or_none()
            if product:
                product.enrichment_status = "failed"
                db.add(product)

            # Update source if all failed
            source = await db.get(Source, batch_id)
            if source:
                source.source_metadata["processing_status"] = "failed"
                source.source_metadata["error"] = str(e)
                db.add(source)

            await db.commit()
            await sync_project_status(db, project_id)


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
            raise HTTPException(
                400, "Missing required fields: mpn and project_id")

        stmt = select(Source).where(
            Source.source_type.in_(
                ["pdf_pending_extraction", "pdf_unstructured_pending", "pdf_multi_pending", "pdf_blind_pending","pdf_blind_extraction"  ]),
            Source.project_id == project_id
        )
        result = await db.execute(stmt)
        sources = result.scalars().all()

        source = None
        for s in sources:
            mpns = s.source_metadata.get('mpns', [])
            single_mpn = s.source_metadata.get('mpn')
            if mpn in mpns or mpn == single_mpn:
                source = s
                break

        if not source:
            raise HTTPException(404, f"PDF source not found for MPN: {mpn}")

        source.status = "processing"
        source.source_metadata["processing_status"] = "processing"
        db.add(source)
        await db.commit()

        # Route to appropriate extraction function
        if source.source_type == "pdf_multi_pending":
            background_tasks.add_task(
                process_multi_pdf_extraction_for_single_mpn,
                source.id, mpn, project_id
            )
        elif source.source_type == "pdf_blind_pending" or source.source_type == "pdf_blind_extraction":
            background_tasks.add_task(
                process_blind_pdf_extraction,
                source.id,
                project_id
            )
        else:
            background_tasks.add_task(
                process_pdf_extraction_for_product,
                source.id, mpn, project_id
            )

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

    # Convert to dict
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
    """Create or update product from extracted data - works for both types."""

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
            project_id=project_id,
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
                'created_at': datetime.utcnow().isoformat(),
                'total': len(request.mpns),
                'extracted': 0,
                'processing_status': 'pending',
                'method': 'web_search'
            }
        )
        db.add(source)
        products_added = 0
        for mpn in request.mpns:
            stmt = select(Product).where(
                Product.project_id == request.project_id,
                Product.product_code == mpn
            )
            result = await db.execute(stmt)
            existing = result.scalar_one_or_none()
            if not existing:
                product = Product(
                    product_code=mpn,
                    product_name=f"{mpn}",
                    mpn=mpn,
                    project_id=request.project_id,
                    workflow_stage='aggregation',
                    enrichment_status='pending',
                    source_url='web_search_pending',
                    completeness_score=0,
                    attributes={}
                )
                db.add(product)
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
            prompt = f"""
You are a product data extraction expert. Extract product information for MPN "{mpn}" from the unstructured document below.
The document is free-form text without structured tables. Extract whatever product specifications you can find.

Document content:
{truncated_text}

Look for:
- Product name/title (usually at the top, in headings, or near the MPN)
- Brand/manufacturer name
- Any technical specifications mentioned in the text
- Product category/hints for taxonomy

Extract the following fields if present. If missing, leave empty:
- product_name
- brand_name
- sku
- taxonomy
- description
- image_url
- specifications (as key-value pairs)

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
                    'completed_at': datetime.utcnow().isoformat()
                })
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
    mpns: str = Form(...),  # Comma-separated MPNs
    project_id: str = Form(...),
    use_case: str = Form(...),
    db: AsyncSession = Depends(get_session)
):
    """
    Upload multiple PDFs with multiple MPNs.
    SAVES ONLY - Extraction happens later from Aggregation tab.
    """
    try:
        # Parse MPNs
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

        # Validate and read all PDFs
        pdf_documents = []
        total_size = 0
        MAX_TOTAL_SIZE = 100 * 1024 * 1024  # 100 MB total

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

        # Create source record with ALL PDFs stored
        source = Source(
            id=batch_id,
            source_type="pdf_multi_pending",
            source_url=f"multi_pdf_{len(pdf_documents)}_files_{len(mpn_list)}_mpns",
            project_id=project_id,
            status="pending",
            content_data=None,  # Will store PDFs separately or in metadata
            source_metadata={
                "mpns": mpn_list,
                "use_case": use_case,
                "pdf_files": [{'filename': p['filename'], 'size': p['size']} for p in pdf_documents],
                "total_pdfs": len(pdf_documents),
                "total_mpns": len(mpn_list),
                "total_size": total_size,
                "created_at": datetime.utcnow().isoformat(),
                "extracted": 0,
                "processing_status": "pending",
                "extraction_type": "multi",
                "is_unstructured": False
            }
        )
        db.add(source)

        # Store PDF contents in a separate table or as binary
        # For simplicity, we can store them in source.content_data as a pickle/zip
        # Or create a separate PDF storage table
        import pickle
        source.content_data = pickle.dumps(pdf_documents)

        # Create placeholder products for each MPN
        products_added = 0
        for mpn in mpn_list:
            stmt = select(Product).where(
                Product.project_id == project_id,
                Product.product_code == mpn
            )
            result = await db.execute(stmt)
            existing = result.scalar_one_or_none()

            if not existing:
                product = Product(
                    product_code=mpn,
                    product_name=f"{mpn}",
                    mpn=mpn,
                    project_id=project_id,
                    workflow_stage="aggregation",
                    enrichment_status="pending",
                    source_url=f"multi_pdf_batch_{batch_id[:8]}",
                    completeness_score=0,
                    attributes={}
                )
                db.add(product)
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
    """
    Background task: Process multiple PDFs for multiple MPNs.
    Intelligently matches MPNs to PDFs based on content.
    """
    async with async_session_factory() as db:
        try:
            logger.info(
                f"🚀 Starting multi-PDF extraction: {len(mpns)} MPNs, {len(pdf_documents)} PDFs")

            source = await db.get(Source, batch_id)
            if source:
                source.status = "processing"
                source.source_metadata["processing_status"] = "processing"
                db.add(source)
                await db.commit()

            # Step 1: Extract text from all PDFs
            pdf_texts = []
            for pdf_doc in pdf_documents:
                try:
                    full_text = ""
                    with pdfplumber.open(BytesIO(pdf_doc['content'])) as pdf:
                        for page in pdf.pages[:15]:  # Limit to 15 pages per PDF
                            page_text = page.extract_text()
                            if page_text:
                                full_text += page_text + "\n"

                    pdf_texts.append({
                        'filename': pdf_doc['filename'],
                        'text': full_text[:20000],  # Limit text per PDF
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

            # Step 2: For each MPN, find the most relevant PDF
            successful = 0
            failed = 0
            results = []

            for mpn in mpns:
                try:
                    logger.info(f"🔍 Processing MPN: {mpn}")

                    # Find product
                    stmt = select(Product).where(
                        Product.project_id == project_id,
                        Product.product_code == mpn
                    )
                    result = await db.execute(stmt)
                    product = result.scalar_one_or_none()

                    if not product:
                        logger.warning(f"Product not found for MPN: {mpn}")
                        failed += 1
                        continue

                    # Update product status
                    product.enrichment_status = "processing"
                    db.add(product)

                    # Find best matching PDF for this MPN
                    best_pdf = None
                    best_score = 0

                    for pdf_text in pdf_texts:
                        if not pdf_text['text']:
                            continue

                        # Score based on MPN presence in text
                        score = 0
                        text_lower = pdf_text['text'].lower()
                        mpn_lower = mpn.lower()

                        # Direct MPN match
                        if mpn_lower in text_lower:
                            score += 100

                        # Partial matches
                        mpn_parts = mpn_lower.replace(
                            '-', ' ').replace('_', ' ').split()
                        for part in mpn_parts:
                            if len(part) > 2 and part in text_lower:
                                score += 20

                        # Filename match
                        if mpn_lower in pdf_text['filename'].lower():
                            score += 50

                        if score > best_score:
                            best_score = score
                            best_pdf = pdf_text

                    if best_pdf and best_score > 0:
                        logger.info(
                            f"Best match: {best_pdf['filename']} (score: {best_score})")

                        truncated_text = best_pdf['text'][:15000]
                        extraction_type = "unstructured"

                        prompt = f"""
You are a product data extraction expert. Extract product information for MPN "{mpn}" from the document below.
The document is a product specification/catalog PDF. Extract whatever product specifications you can find.

Document content:
{truncated_text}

Look for:
- Product name/title
- Brand/manufacturer name
- Technical specifications (dimensions, weight, materials, features)
- Product category/taxonomy

Extract the following fields if present. If missing, leave empty:
- product_name
- brand_name
- sku
- taxonomy
- description
- image_url
- specifications (as key-value pairs)

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

                    # Update product as failed
                    stmt = select(Product).where(
                        Product.project_id == project_id,
                        Product.product_code == mpn
                    )
                    result = await db.execute(stmt)
                    product = result.scalar_one_or_none()
                    if product:
                        product.enrichment_status = "failed"
                        db.add(product)

            # Update source with final results
            source = await db.get(Source, batch_id)
            if source:
                source.status = "completed" if successful > 0 else "failed"
                source.source_metadata.update({
                    "processing_status": "completed" if successful > 0 else "failed",
                    "successful": successful,
                    "failed": failed,
                    "results": results,
                    "completed_at": datetime.utcnow().isoformat()
                })
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
