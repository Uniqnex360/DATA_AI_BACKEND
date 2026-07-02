from typing import Dict, Optional
from app.rules.rule_engine import RuleEngine
from sqlalchemy.ext.asyncio import AsyncSession
import logging
logger = logging.getLogger(__name__)
class PDFPromptService:
    def __init__(self, db: AsyncSession, project_id: str = None):
        self.db = db
        self.project_id = project_id
        self.rule_engine = RuleEngine(db) if db else None
    async def get_prompt(
        self, 
        stage: str, 
        use_case: str, 
        context: Dict
    ) -> Optional[str]:
        if not self.rule_engine:
            return None
        try:
            return await self.rule_engine.get_active_prompt(
                stage=stage,
                operation_mode="pdf_extraction",
                use_case=use_case,
                context=context,
            )
        except Exception as e:
            logger.warning(f"Failed to get {stage} prompt: {e}")
            return None
    async def get_identification_prompt(
        self,
        pdf_text: str,
        filename: str,
        product_hint: str,
        use_case: str
    ) -> Optional[str]:
        context = {
            "pdf_text": pdf_text[:15000],
            "filename": filename,
            "product_hint": product_hint,
            "text_sample": pdf_text[:2000]
        }
        return await self.get_prompt("pdf_identification", use_case, context)
    async def get_extraction_prompt(
        self,
        pdf_text: str,
        mpn: str,
        use_case: str,
        is_unstructured: bool = False
    ) -> Optional[str]:
        stage = "pdf_unstructured" if is_unstructured else "pdf_extraction"
        context = {
            "pdf_text": pdf_text[:15000],
            "mpn": mpn,
            "text_sample": pdf_text[:2000]
        }
        return await self.get_prompt(stage, use_case, context)
    async def get_blind_extraction_prompt(
        self,
        pdf_text: str,
        product_info: Dict,
        use_case: str
    ) -> Optional[str]:
        context = {
            "pdf_text": pdf_text[:12000],
            "title": product_info.get('title', 'Unknown Product'),
            "context": product_info.get('context', ''),
            "text_sample": pdf_text[:2000]
        }
        return await self.get_prompt("pdf_blind_extraction", use_case, context)
    # async def get_structured_extraction_prompt(
    #     self, 
    #     pdf_text: str, 
    #     tables: str, 
    #     mpn: str, 
    #     use_case: str
    # ) -> Optional[str]:
    #     if not self.rule_engine:
    #         return None
    #     try:
    #         context = {
    #             "pdf_text": pdf_text,
    #             "tables": tables,
    #             "mpn": mpn,
    #             "text_sample": pdf_text[:2000]
    #         }
    #         return await self.rule_engine.get_active_prompt(
    #             stage="pdf_structured",
    #             operation_mode="pdf_extraction",
    #             use_case=use_case,
    #             context=context,
    #         )
    #     except Exception as e:
    #         logger.warning(f"Failed to get structured extraction prompt: {e}")
    #         return None
    async def get_structured_extraction_prompt(
        self, 
        pdf_text: str, 
        tables: str, 
        mpn: str, 
        use_case: str
    ) -> Optional[str]:
        if not self.rule_engine:
            return None
        try:
            # Smart truncation: find MPN and take surrounding context
            mpn_lower = mpn.lower()
            text_lower = pdf_text.lower()
            mpn_pos = text_lower.find(mpn_lower)
            
            if mpn_pos != -1:
                start = max(0, mpn_pos - 4000)
                end = min(len(pdf_text), mpn_pos + 6000)
                truncated_text = pdf_text[start:end]
            else:
                truncated_text = pdf_text[:10000]
            
            context = {
                "pdf_text": truncated_text,
                "tables": tables[:3000],
                "mpn": mpn,
                "text_sample": pdf_text[:2000]
            }
            return await self.rule_engine.get_active_prompt(
                stage="pdf_structured",
                operation_mode="pdf_extraction",
                use_case=use_case,
                context=context,
            )
        except Exception as e:
            logger.warning(f"Failed to get structured extraction prompt: {e}")
            return None
    async def get_unstructured_extraction_prompt(
        self, 
        pdf_text: str, 
        mpn: str, 
        use_case: str
    ) -> Optional[str]:
        if not self.rule_engine:
            return None
        try:
            context = {
                "pdf_text": pdf_text,
                "mpn": mpn,
                "text_sample": pdf_text[:2000]
            }
            return await self.rule_engine.get_active_prompt(
                stage="pdf_unstructured",
                operation_mode="pdf_extraction",
                use_case=use_case,
                context=context,
            )
        except Exception as e:
            logger.warning(f"Failed to get unstructured extraction prompt: {e}")
            return None
