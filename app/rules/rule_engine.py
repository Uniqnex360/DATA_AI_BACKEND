import logging
from typing import Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select,func
from datetime import datetime
from app.core.config import settings
from app.models.business_rule import BusinessRule, RulePrompt, RuleStatus
logger = logging.getLogger("rule_engine")
class RuleEngine:
    def __init__(self, db: AsyncSession):
        self.db = db
    # In rule_engine.py - get_active_prompt method

    async def get_active_prompt(
        self,
        *,
        stage: str,
        operation_mode: str,
        use_case: str,
        context: Dict[str, Any],
        allow_fallback: bool = False,
    ) -> str:
        try:
            stmt = (
                select(RulePrompt)
                .join(BusinessRule, RulePrompt.rule_id == BusinessRule.id)
                .where(
                    BusinessRule.status == RuleStatus.ACTIVE,
                    RulePrompt.status == RuleStatus.ACTIVE,
                    RulePrompt.stage == stage,
                    func.lower(BusinessRule.operation_mode) == operation_mode.lower(),
                    func.lower(BusinessRule.use_case) == use_case.lower()
                )
                .order_by(RulePrompt.priority.desc(), RulePrompt.updated_at.desc())
            )
            
            result = await self.db.execute(stmt)
            prompts = result.scalars().all()
            
            # ========== FALLBACK FOR ENRICHMENT ==========
            if not prompts and operation_mode == "enrichment":
                # Try base use_case without "and existing attribute validation"
                if "and existing attribute validation" in use_case:
                    fallback_use_case = use_case.replace(" and existing attribute validation", "")
                    stmt = (
                        select(RulePrompt)
                        .join(BusinessRule, RulePrompt.rule_id == BusinessRule.id)
                        .where(
                            BusinessRule.status == RuleStatus.ACTIVE,
                            RulePrompt.status == RuleStatus.ACTIVE,
                            RulePrompt.stage == stage,
                            func.lower(BusinessRule.operation_mode) == operation_mode.lower(),
                            func.lower(BusinessRule.use_case) == fallback_use_case.lower()
                        )
                        .order_by(RulePrompt.priority.desc(), RulePrompt.updated_at.desc())
                    )
                    result = await self.db.execute(stmt)
                    prompts = result.scalars().all()
                    if prompts:
                        logger.info(f"Using fallback use_case: {fallback_use_case}")
            # ============================================
            
            if not prompts:
                if allow_fallback:
                    logger.warning(f"No active prompt for {stage}/{operation_mode}/{use_case}")
                    return ""
                raise ValueError(f"No ACTIVE prompt configured for stage='{stage}', operation_mode='{operation_mode}', use_case='{use_case}'")
            
            prompt = prompts[0]
            rendered = prompt.prompt_text.format(**context)
            
            prompt.execution_count += 1
            prompt.last_executed_at = datetime.utcnow()
            self.db.add(prompt)
            
            return rendered
            
        except Exception:
            logger.error("RuleEngine failed to get active prompt", exc_info=True)
            raise