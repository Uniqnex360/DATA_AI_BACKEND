from email.policy import HTTP
import trace

from curl_cffi import AsyncSession
from fastapi import APIRouter, BackgroundTasks,Depends, HTTPException
from datetime import datetime
import aiohttp
from openai import timeout
from app.aggregation.services.pdf_service import PDFExtractionService
from app.core.database import get_session
from app.llm import call_llm_with_schema
from app.models.pipeline import Source
from app.models.product import Product
from app.schemas.pdf_extraction import FreshAggregationRequest
import uuid
import logging
from typing import List,Optional,Dict
from app.search.searxng_service import SearXNGSearchService
from app.core.database import async_session_factory
logger=logging.getLogger('pdf extraction')
router = APIRouter()
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
            'products':[]
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
        extracted=await call_llm_with_schema(prompt=prompt,response_model=schema,llm_provider='claude',estimated_tokens=4000)
        if extracted:
            extracted['mpn']=mpn
            extracted['source_url']=source_url
            return extracted
    except Exception as e:
        logger.error(f"Claude extraction failed for {mpn}: {e}")
    return None

async def process_fresh_pdf_aggregation(batch_id:str,mpns:List[str],project_id:str):
    try:
        async with async_session_factory() as db:
            try:
                products=[]
                successful=0
                failed_mpns=[]
                for idx,mpn in enumerate(mpns):
                    try:
                        logger.info(f"Processing MPN {idx+1}/{len(mpns)}: {mpn}")
                        pdf_urls=await search_pdfs_url(mpn,brand='')
                        if not pdf_urls:
                            logger.warning(f"No PDF urls  found for {mpn}")
                            failed_mpns.append(mpn)
                            continue
                        product_data=None
                        for pdf_url in pdf_urls:
                            pdf_text=await download_and_extract_pdf(pdf_url)
                            if pdf_text:
                                product_data=await extract_product_with_claude(mpn,pdf_text,pdf_url)
                                if product_data:
                                    break
                        if product_data:
                            product=Product(
                                product_code=mpn,
                                product_name=product_data.get('product_name',f"Product {mpn}"),
                                brand_name=product_data.get("brand_name", ""),
                                mpn=mpn,
                                sku=product_data.get("sku", ""),
                                taxonomy=product_data.get('taxonomy',''),
                                description=product_data.get('description',''),
                                image_url_1=product_data.get('image_url'),
                                project_id=project_id,
                                workflow_stage='aggregation',
                                enrichment_status='pending',
                                attributes=product_data.get('specifications',{}),
                                source_url=product_data.get('source_url', ""),
                                completeness_score=0
                            )
                            db.add(product)
                            products.append(product_data)
                            successful+=1
                            logger.info(f"Successfully extracted product for {mpn}")
                        else:
                            failed_mpns.append(mpn)
                            logger.warning(f"Failed to extract product for {mpn}")
                        
                    except Exception as e:
                        logger.error(f"Error processing MPN {mpn}: {e}")
                        failed_mpns.append(mpn)
                    if (idx + 1) % 3 == 0 or idx + 1 == len(mpns):
                        source=await db.get(Source,batch_id)
                        if source:
                            source.source_metadata['successful']=successful
                            source.source_metadata['failed']=len(failed_mpns)
                            source.source_metadata['current_index']=idx+1
                            source.source_metadata['products']=products
                            source.source_metadata['failed_mpns']=failed_mpns
                            db.add(source)
                            await db.commit()
                source=await db.get(Source,batch_id)
                if source:
                    source.status='completed'
                    source.source_metadata['completed_at']=datetime.utcnow().isoformat()
                    source.source_metadata['products']=products
                    source.source_metadata['failed_mpns']=failed_mpns
                    db.add(source)
                    await db.commit()
                logger.info(f"Fresh aggregation completed :{successful} succesful,{len(failed_mpns)} failed")
                            
            except Exception as e:
                logger.error(f"Fresh PDF aggregation failed :{e}")
                source=await db.get(Source,batch_id)
                if source:
                    source.status='failed'
                    source.source_metadata['error']=str(e)
                    db.add(source)
                    await db.commit()
    except Exception as e:
        logger.error(f"Fresh PDF aggregation failed :{e}")
    
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