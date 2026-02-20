from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status, Form, UploadFile, File, Request,Query
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from app.models.pipeline import AuditTrail, CleansingIssue, RawExtraction, Source, SourcePriority
from app.core.database import get_session, async_session_factory
from app.models.product import Product
from typing import List
import logging
import json
import io
from datetime import datetime
from app.aggregation import aggregate_product
from app.schemas.extraction import ExtractionRequest, SourceMetricsResponse, SourceResponse
from app.schemas.pipeline import SourcePriorityResponse
from app.utils import is_invalid
import pandas as pd
import os
logger = logging.getLogger("extraction_router")
router = APIRouter()


@router.get("/", response_model=List[SourceResponse])
async def getAllSources(db: AsyncSession = Depends(get_session)):
    try:
        statement = select(Source).order_by(Source.uploaded_at.desc())
        result = await db.execute(statement)
        sources = result.scalars().all()
        return sources
    except Exception as e:
        logger.error(f"Failed to fetch sources: {str(e)}")
        raise HTTPException(
            status_code=500, detail="Could not retrieve import history")


@router.post("/", status_code=status.HTTP_202_ACCEPTED)
async def extract_from_source(
    payload: ExtractionRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session)
):
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        formatted_name = f"Manual_{timestamp}_{payload.sourceUrl}"
        new_source = Source(
            source_type=payload.sourceType,
            source_url=formatted_name,
            project_id=payload.projectId,
            status="processing",
            content_data=payload.content.encode('utf-8'),
            source_metadata={"raw_length": len(payload.content)}
        )
        db.add(new_source)
        await db.commit()
        await db.refresh(new_source)
        background_tasks.add_task(
            run_extraction_task,
            str(new_source.id),
            payload.content,
        )
        return {
            "status": "accepted",
            "source_id": str(new_source.id),
            "message": "AI pipeline initialized in background"
        }
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to initialize extraction: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="System failed to initialize the extraction pipeline"
        )


@router.get("/priorities/{project_id}", response_model=List[SourcePriorityResponse], status_code=status.HTTP_200_OK)
async def get_project_priorities(project_id: str, db: AsyncSession = Depends(get_session)):
    try:
        statement = (
            select(SourcePriority)
            .where(SourcePriority.project_id == project_id)
            .order_by(SourcePriority.priority_rank.asc())
        )
        result = await db.execute(statement)
        priorities = result.scalars().all()
        logger.info(
            f"Retrieved {len(priorities)} priority rankings for project {project_id}")
        return priorities
    except Exception as e:
        logger.error(f"DATABASE ERROR in get_project_priorities: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal system error while retrieving source rankings"
        )


@router.get('/project/{project_id}', response_model=List[SourceResponse])
async def get_sources_by_project(project_id: str, db: AsyncSession = Depends(get_session)):
    try:
        if project_id is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Project id is required")
        statement = select(Source).where(Source.project_id ==
                                         project_id).order_by(Source.uploaded_at.desc())
        result = await db.execute(statement)
        sources = result.scalars().all()
        return sources
    except Exception as e:
        logger.error(f"Failed to fetch project sources:{e}")
        return []


# @router.get("/{source_id}/download")
# async def download_file(
#     source_id: str,
#     file_type: str = Query("input", alias="type"), 
#     db: AsyncSession = Depends(get_session)
# ):
#     try:
#         source = await db.get(Source, source_id)
#         if not source:
#             raise HTTPException(
#                 status_code=404, detail="Source record not found")
#         if file_type == 'input':
#             if source.content_data:
#                 return StreamingResponse(
#                     io.BytesIO(source.content_data),
#                     media_type="application/octet-stream",
#                     headers={
#                         "Content-Disposition": f"attachment; filename=Input_{source.source_url}"}
#                 )
#             elif not source.content_data:
#                 output = io.StringIO()
#                 output.write(f"--- MANUAL INGESTION LOG ---\n")
#                 output.write(f"SKU: {source.source_url}\n")
#                 output.write(f"Timestamp: {source.uploaded_at}\n")
#                 return StreamingResponse(
#                     io.BytesIO(output.getvalue().encode()),
#                     media_type="text/plain",
#                     headers={
#                         "Content-Disposition": f"attachment; filename=Manual_Input_{source_id}.txt"}
#                 )
#             else:
#                 raise HTTPException(
#                     status_code=404, detail="No source data found in database")
#         elif file_type == 'output':
#             stmt = select(RawExtraction).where(
#                 RawExtraction.source_id == source_id)
#             result = await db.execute(stmt)
#             extractions = result.scalars().all()
#             if not extractions:
#                 raise HTTPException(
#                     status_code=404, detail="No AI data generated yet")

#             def flatten_dict(d, parent_key='', sep='_'):

#                 items = []
#                 for k, v in d.items():
#                     new_key = f"{parent_key}{sep}{k}" if parent_key else k
#                     if isinstance(v, dict):
#                         items.extend(flatten_dict(v, new_key, sep=sep).items())
#                     elif isinstance(v, list):
#                         items.append((new_key, ', '.join(map(str, v))))
#                     else:
#                         items.append((new_key, v))
#                 return dict(items)
#             rows = []
#             for idx, ext in enumerate(extractions):
#                 flattened_attrs = flatten_dict(ext.raw_attributes)
#                 rows.append({
#                     "System_ID": f"PID-{1000 + idx}",
#                     "MPN": ext.product_keys.get('sku') or ext.product_keys.get('mpn') or "UNKNOWN",
#                     **flattened_attrs
#                 })
#             excel_buffer = io.BytesIO()
#             df = pd.DataFrame(rows)
#             df.columns = [col.replace('_', ' ').title() for col in df.columns]
#             df.to_excel(excel_buffer, index=False, engine='openpyxl')
#             excel_buffer.seek(0)
#             return StreamingResponse(
#                 excel_buffer,
#                 media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
#                 headers={
#                     "Content-Disposition": f"attachment; filename=AI_Output_{source_id}.xlsx"}
#             )
#         raise HTTPException(status_code=400, detail="Invalid file_type")
#     except HTTPException as he:
#         raise he
#     except Exception as e:
#         logger.error(f"Memory Download Error: {str(e)}")
#         raise HTTPException(
#             status_code=500, detail="Internal server error preparing your download")

@router.get("/{source_id}/download")
async def download_file(
    source_id: str,
    download_type: str = Query("input", alias="type"), 
    db: AsyncSession = Depends(get_session)
):
    try:
        source = await db.get(Source, source_id)
        if not source:
            raise HTTPException(status_code=404, detail="Source record not found")

        if download_type == 'input':
            if source.content_data:
                return StreamingResponse(
                    io.BytesIO(source.content_data),
                    media_type="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename=Input_{source.source_url}"}
                )
            output = io.StringIO()
            output.write(f"SKU: {source.source_url}\nUploaded: {source.uploaded_at}")
            return StreamingResponse(io.BytesIO(output.getvalue().encode()), media_type="text/plain")

        elif download_type == 'output':
            stmt = select(Product).where(Product.project_id == source.project_id,Product.source_url==source.source_url)
            result = await db.execute(stmt)
            products = result.scalars().all()

            if not products:
                raise HTTPException(status_code=404, detail="No enriched data found for this project")

            def flatten_logic(data, prefix=''):
                items = []
                
                if isinstance(data, dict):
                    for k, v in data.items():
                        new_key = f"{prefix} {k}".strip().replace('_', ' ').title()
                        
                        if isinstance(v, (dict, list)):
                            items.extend(flatten_logic(v, new_key).items())
                        else:
                            items.append((new_key, v))
                            
                elif isinstance(data, list):
                    for i, v in enumerate(data):
                        new_prefix = f"{prefix} {i+1}" if len(data) > 1 else prefix
                        
                        if isinstance(v, (dict, list)):
                            items.extend(flatten_logic(v, new_prefix).items())
                        else:
                            items.append((new_prefix, v))
                            
                return dict(items)

            rows = []
            for p in products:
                row = {
                    "SKU": p.product_code,
                    "Name": p.product_name,
                    "Brand": p.brand_name,
                    "Completeness": f"{p.completeness_score}%"
                }
                
                if p.attributes:
                    flattened = flatten_logic(p.attributes)
                    row.update(flattened)
                
                rows.append(row)

            df = pd.DataFrame(rows)
            excel_buffer = io.BytesIO()
            with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                df.to_excel(writer, index=False, sheet_name='Enriched Data')
            
            excel_buffer.seek(0)
            
            return StreamingResponse(
                excel_buffer,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f"attachment; filename=Enriched_Data_{source_id}.xlsx"}
            )

    except Exception as e:
        logger.error(f"Download Error: {str(e)}")
        raise HTTPException(status_code=500, detail="Error generating download")

@router.post("/batch-aggregate", status_code=status.HTTP_202_ACCEPTED)
async def batch_aggregate(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    projectId: str = Form(...),
    db: AsyncSession = Depends(get_session)
):
    try:
        content = await file.read()
        timestamp = datetime.now().strftime("%Y-%m-%d_%H:%M:%S")
        formatted_filename = f"Import_{timestamp}"

        if not projectId:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Project ID is required. Please select a project first."
            )
        project_id = projectId
        logger.info(f"Using project_id (snake_case): {project_id}")
        new_source = Source(
            source_type="excel" if file.filename.endswith(
                ('.xlsx', '.xls')) else "csv",
            source_url=formatted_filename,
            project_id=project_id,
            content_data=content,
            status="processing"
        )
        db.add(new_source)
        await db.commit()
        await db.refresh(new_source)
        if file.filename.endswith(('.xlsx', '.xls')):
            df = pd.read_excel(io.BytesIO(content))
        else:
            df = pd.read_csv(io.BytesIO(content))
        df.columns = df.columns.str.strip().str.lower()
        df_dict = df.to_dict('records')
        background_tasks.add_task(
            run_extraction_task,
            str(new_source.id),
            json.dumps(df_dict)
        )
        return {
            "status": "accepted",
            "batch_id": str(new_source.id),
            "message": f"Batch processing started for {len(df_dict)} products"
        }
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to initialize batch processing: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="System failed to initialize batch processing"
        )


@router.get("/batch-status/{batch_id}")
async def get_batch_status(batch_id: str, db: AsyncSession = Depends(get_session)):
    try:
        source = await db.get(Source, batch_id)
        if not source:
            raise HTTPException(status_code=404, detail="Batch not found")
        return {
            "batch_id": str(source.id),
            "status": source.status,
            "metadata": source.source_metadata,
            "created_at": source.uploaded_at
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching batch status: {str(e)}")
        raise HTTPException(
            status_code=500, detail="Failed to fetch batch status")


@router.get("/{source_id}/metrics", response_model=SourceMetricsResponse)
async def get_source_metrics(source_id: str, db: AsyncSession = Depends(get_session)):
    try:
        source = await db.get(Source, source_id)
        if not source:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Source record not found"
            )
        statement = select(RawExtraction).where(
            RawExtraction.source_id == source_id)
        result = await db.execute(statement)
        extractions = result.scalars().all()
        if not extractions:
            return {
                "avgConfidence": 0.0,
                "completeness": 0.0,
                "totalAttributes": 0
            }
        total_conf = sum(ext.confidence for ext in extractions)
        avg_conf = total_conf / len(extractions)
        unique_attributes = set()
        for ext in extractions:
            if isinstance(ext.raw_attributes, dict):
                unique_attributes.update(ext.raw_attributes.keys())
        total_attrs_count = len(unique_attributes)
        TARGET_ATTR_COUNT = 20
        completeness_score = min(total_attrs_count / TARGET_ATTR_COUNT, 1.0)
        logger.info(
            f"Metrics generated for source {source_id}: {total_attrs_count} attrs found")
        return {
            "avgConfidence": round(avg_conf, 2),
            "completeness": round(completeness_score, 2),
            "totalAttributes": total_attrs_count
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"CRITICAL ERROR calculating metrics for {source_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Analytics engine failed to calculate metrics"
        )

# async def run_extraction_task(source_id: str, content: str):
#     async with async_session_factory() as db_session:
#         try:
#             source = await db_session.get(Source, source_id)
#             if not source:
#                 return
#             items_to_process = []
#             try:
#                 items_to_process = json.loads(content)
#             except:
#                 lines = content.split('\n')
#                 current_manual_item = {}
#                 for line in lines:
#                     if ':' in line:
#                         k, v = line.split(':', 1)
#                         current_manual_item[k.strip().lower()] = v.strip()
#                 if current_manual_item:
#                     items_to_process.append(current_manual_item)
#             successful=0
#             failed=0
#             for item in items_to_process:
#                 try:
#                     sku = (item.get('mpn') or item.get('sku') or "UNKNOWN").upper()
#                     title = item.get('title') or item.get('product_name')
#                     brand=item.get('brand') or item.get('brand_name') or ""
#                     if not sku:
#                         failed+=1
#                         continue
#                     stmt=select(Product).where(Product.product_code==sku)
#                     prod_result=await db_session.execute(stmt)
#                     product=prod_result.scalars().first()
#                     raw_attributes = {k: v for k, v in item.items() if v and k not in ['mpn', 'sku', 'product_code']}
#                     if not product:
#                         product=Product(
#                             product_code=sku,
#                             product_name=title or sku,
#                             brand_name=brand,
#                             mpn=sku,
#                             project_id=source.project_id,
#                             attributes=raw_attributes,
#                             enrichment_status='pending',
#                             completeness_score=10,
#                         )
#                         db_session.add(product)
#                         logger.info(f"Product created ;{sku}")
#                     db_session.add(RawExtraction(source_id=source_id,product_keys={'sku':sku,"mpn":sku},raw_attributes=raw_attributes,confidence=0.5))
#                     successful+=1
#                 except Exception as e:
#                     logger.error(f"Failed to process item:{e}")
#                     failed+=1
#                     continue

#                 result = await aggregate_product(mpn=sku, title=title)
#                 if result.get('status') == 'success' and sku:
#                     ai_data = result.get('golden_record', {}).get('attributes', {})
#                     db_session.add(RawExtraction(
#                         source_id=source.id,
#                         product_keys={"sku": sku, "mpn": sku},
#                         raw_attributes=ai_data,
#                         confidence=result.get('golden_record', {}).get('confidence', 0.9)
#                     ))
#                     for attr_name, attr_value in ai_data.items():
#                         val_str = str(attr_value)
#                         if is_invalid(val_str):
#                             db_session.add(CleansingIssue(
#                                 product_id=sku, attribute_name=attr_name, issue_type="invalid",
#                                 details=f"Placeholder detected: '{val_str}'", resolved=False
#                             ))
#                     stmt = select(Product).where(Product.product_code == sku)
#                     prod_result = await db_session.execute(stmt)
#                     product = prod_result.scalars().first()
#                     if not product:
#                         product = Product(
#                             product_code=sku, product_name=title or sku,
#                             brand_name=item.get('brand'), project_id=source.project_id,
#                             attributes=ai_data, enrichment_status='completed',
#                             completeness_score=min(len(ai_data) * 5, 100)
#                         )
#                     else:
#                         product.attributes = ai_data
#                         product.enrichment_status = 'completed'
#                         product.completeness_score = min(len(ai_data) * 5, 100)
#                     db_session.add(product)
#             source.status = "completed"
#             source.uploaded_at = datetime.utcnow()
#             db_session.add(source)
#             db_session.add(AuditTrail(
#                 product_id="BATCH_UPLOAD" if len(items_to_process) > 1 else items_to_process[0].get('mpn', 'MANUAL'),
#                 stage="extraction",
#                 attribute_name="ingestion",
#                 selected_value="Success",
#                 sources_used=source.source_url,
#                 reason=f"Successfully processed {len(items_to_process)} product(s) from this source."
#             ))
#             current_project_id = source.project_id or "default-project"
#             prio_stmt = select(SourcePriority).where(SourcePriority.source_id == str(source.id))
#             prio_check = await db_session.execute(prio_stmt)
#             if not prio_check.scalars().first():
#                 db_session.add(SourcePriority(
#                     project_id=str(current_project_id),
#                     source_id=str(source.id),
#                     priority_rank=1
#                 ))
#             await db_session.commit()
#             logger.info(f"✓ Entire batch {source_id} finalized successfully.")
#         except Exception as e:
#             await db_session.rollback()
#             logger.error(f"Pipeline crashed for batch {source_id}: {str(e)}")
#             try:
#                 async with async_session_factory() as error_session:
#                     s = await error_session.get(Source, source_id)
#                     if s:
#                         s.status = "failed"
#                         db_session.add(s)
#                         await error_session.commit()
#             except: pass


async def run_extraction_task(source_id: str, content: str):

    async with async_session_factory() as db_session:
        try:
            source = await db_session.get(Source, source_id)
            if not source:
                return

            items_to_process = []
            try:
                items_to_process = json.loads(content)
            except:

                lines = content.split('\n')
                current_manual_item = {}
                for line in lines:
                    if ':' in line:
                        k, v = line.split(':', 1)
                        current_manual_item[k.strip().lower()] = v.strip()
                if current_manual_item:
                    items_to_process.append(current_manual_item)

            successful = 0
            failed = 0

            for item in items_to_process:
                try:

                    sku = (item.get('mpn') or item.get('sku') or item.get(
                        'product_code') or "").strip().upper()
                    title = item.get('title') or item.get(
                        'product_name') or item.get('name') or ""
                    brand = item.get('brand') or item.get('brand_name') or ""

                    if not sku:
                        failed += 1
                        continue

                    stmt = select(Product).where(Product.product_code == sku)
                    prod_result = await db_session.execute(stmt)
                    product = prod_result.scalars().first()

                    raw_attributes = {k: v for k, v in item.items() if v and k not in [
                        'mpn', 'sku', 'product_code']}

                    if not product:

                        product = Product(
                            product_code=sku,
                            product_name=title or sku,
                            brand_name=brand,
                            mpn=sku,
                            project_id=source.project_id,
                            source_url=source.source_url,
                            attributes=raw_attributes,
                            enrichment_status='pending',
                            completeness_score=10
                        )
                        db_session.add(product)
                        logger.info(f"Created product: {sku}")
                    else:
                        product.source_url = source.source_url
                        product.attributes = {**product.attributes, **raw_attributes}
                        product.enrichment_status = 'pending'
                        db_session.add(product)
                        logger.info(f"Updated product: {sku}")

                    db_session.add(RawExtraction(
                        source_id=source.id,
                        product_keys={"sku": sku, "mpn": sku},
                        raw_attributes=raw_attributes,
                        confidence=0.0,
                        extracted_at=datetime.utcnow()
                    ))

                    successful += 1

                except Exception as e:
                    logger.error(f"Failed to process item: {e}")
                    failed += 1
                    continue

            source.status = "completed"
            source.source_metadata = {
                "total": len(items_to_process),
                "successful": successful,
                "failed": failed,
                "aggregation_status": "pending"
            }
            db_session.add(source)

            db_session.add(AuditTrail(
                product_id="BATCH_UPLOAD",
                stage="extraction",
                attribute_name="ingestion",
                selected_value="Success",
                sources_used=source.source_url,
                reason=f"Imported {successful} products (awaiting aggregation)"
            ))

            await db_session.commit()
            logger.info(
                f"✓ Import complete for {source_id}: {successful} products stored")

        except Exception as e:
            await db_session.rollback()
            logger.error(f"Import failed for {source_id}: {str(e)}")

            try:
                async with async_session_factory() as error_session:
                    s = await error_session.get(Source, source_id)
                    if s:
                        s.status = "failed"
                        s.source_metadata = {"error": str(e)}
                        error_session.add(s)
                        await error_session.commit()
            except:
                pass


@router.post('/aggregate/{source_id}', status_code=status.HTTP_202_ACCEPTED)
async def trigger_aggregation(source_id: str, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_session)):
    try:
        source = await db.get(Source, source_id)
        if not source:
            raise HTTPException(status_code=404, detail='Source not found')
        if source.status != "completed":
            raise HTTPException(
                status_code=400, detail="Source must be imported first")
        source.source_metadata = {
            **source.source_metadata,
            'aggregation_status': 'processing'
        }
        db.add(source)
        await db.commit()
        background_tasks.add_task(run_aggregation_task, str(source.id))
        return {
            'status': 'accepted',
            'message': "Aggregation started in the background",
            'source_id': source.id
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to trigger aggregation:{str(e)}")
        raise HTTPException(
            status_code=500, detail="Failed to start aggregation")


async def run_aggregation_task(source_id: str):
    async with async_session_factory() as db_session:
        try:
            source = await db_session.get(Source, source_id)
            if not source:
                return
            stmt = select(RawExtraction).where(
                RawExtraction.source_id == source_id)
            result = await db_session.execute(stmt)
            extractions = result.scalars().all()
            successful = 0
            failed = 0
            total = len(extractions)
            for idx, extraction in enumerate(extractions):
                try:
                    sku = extraction.product_keys.get('sku', '') or extraction.product_keys.get('mpn', '')
                    prod_stmt=select(Product).where(Product.product_code==sku)
                    prod_result=await db_session.execute(prod_stmt)
                    product=prod_result.scalars().first()
                    if not product:
                        continue
                    logger.info(f"Aggregating {idx+1}/{total}: {sku}")
                    aggregation_result=await aggregate_product(mpn=sku,title=product.product_name)
                    if aggregation_result.get('status')=='success':
                        ai_data=aggregation_result.get('golden_record',{}).get('attributes',{})
                        confidence=aggregation_result.get('golden_record',{}).get('confidence',0.5)
                        extraction.raw_attributes=ai_data
                        extraction.confidence=confidence
                        extraction.extracted_at=datetime.utcnow()
                        db_session.add(extraction)
                        product.attributes={**product.attributes,**ai_data}
                        product.enrichment_status='completed'
                        product.completeness_score=min(len(ai_data)*5,100)
                        db_session.add(product)
                        for attr_name,attr_value in ai_data.items():
                            if is_invalid(str(attr_value)):
                                db_session.add(CleansingIssue(product_id=sku,attribute_name=attr_name,issue_type='invalid',details=f"Placeholder detected `{attr_value}`",resolved=False))
                        successful+=1
                        logger.info(f"Aggregated {sku}: {len(ai_data)} attributes")
                    else:
                        failed+=1
                        product.enrichment_status='Failed'
                        db_session.add(product)
                        logger.warning(f"Aggregation failed for {sku}")
                except Exception as e:
                    logger.error(f"Aggregation error for {sku}:{e}")
                    failed+=1
                    continue
            source.source_metadata = {
                **source.source_metadata,
                "aggregation_status": "completed",
                "aggregated_successful": successful,
                "aggregated_failed": failed,
                "last_aggregation_time": datetime.utcnow().isoformat()
            }
            db_session.add(source)
            db_session.add(AuditTrail(
                product_id="BATCH_AGGREGATION",
                stage="aggregation",
                attribute_name="truth_engine",
                selected_value="Completed",
                sources_used=source.source_url,
                reason=f"AI aggregated {successful}/{total} products successfully"
            ))
            await db_session.commit()
            logger.info(f"Aggregation complete :{successful}/{total} successful,{failed} failed")

        except Exception as e:
            await db_session.rollback()
            logger.error(f"Aggregation task failed for {source_id}:{str(e)}")

