from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, func, and_, or_
from typing import List, Optional
from datetime import datetime
from app.core.database import get_session
from app.models.business_rule import BusinessRule, RuleCategory, RulePrompt, RuleStatus
from app.schemas.business_rule import (BusinessRuleCreate, BusinessRuleUpdate, BusinessRuleResponse,
                                       BusinessRuleListResponse, RulePromptResponse, RulePromptCreate, RulePromptUpdate,)
import logging
from sqlalchemy.orm import selectinload
logger = logging.getLogger("business_rules")
router = APIRouter()


@router.post("/", response_model=BusinessRuleResponse, status_code=status.HTTP_201_CREATED)
async def create_business_rule(
    rule: BusinessRuleCreate,
    db: AsyncSession = Depends(get_session)
):
    try:
        base_rule_id = BusinessRule.generate_rule_id(rule.title)
        rule_id = base_rule_id
        counter = 1
        while True:
            stmt = select(BusinessRule).where(BusinessRule.rule_id == rule_id)
            existing = await db.execute(stmt)
            if existing.scalars().first():
                break
            rule_id = f"{base_rule_id}_counter"
            counter += 1
            new_rule = BusinessRule(
                **rule.dict(),
                rule_id=rule_id,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            )
            db.add(new_rule)
            await db.commit()
            # await db.refresh(new_rule)
            stmt = select(BusinessRule).options(selectinload(BusinessRule.prompts)).where(BusinessRule.id == new_rule.id)
            result = await db.execute(stmt)
            created_rule_with_prompts = result.scalars().one()
            logger.info(f"Created business rule: {new_rule.rule_id}")   
            return created_rule_with_prompts
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to create rule: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create rule: {str(e)}"
        )


@router.get("/", response_model=BusinessRuleListResponse)
async def get_all_business_rules(
    category: Optional[RuleCategory] = None,
    status: Optional[RuleStatus] = None,
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_session)
):
    try:
        stmt = select(BusinessRule).options(selectinload(BusinessRule.prompts))
        conditions = []
        if category:
            conditions.append(BusinessRule.category == category)
        if status:
            conditions.append(BusinessRule.status == status)
        if search:
            search_pattern = f"%{search}%"
            conditions.append(
                or_(
                    BusinessRule.title.ilike(search_pattern),
                    BusinessRule.description.ilike(search_pattern),
                )
            )
        if conditions:
            stmt = stmt.where(and_(*conditions))
        result = await db.execute(stmt)
        rules = result.scalars().unique().all()
        for rule in rules:
            prompt_stmt = select(RulePrompt).where(
                RulePrompt.rule_id == rule.id).order_by(RulePrompt.priority.desc())
            prompt_result = await db.execute(prompt_stmt)
            rule.prompts = prompt_result.scalars().all()
        count_stmt = select(func.count(BusinessRule.id))
        if conditions:
            count_stmt = count_stmt.where(and_(*conditions))
        total_result = await db.execute(count_stmt)
        total = total_result.scalar_one()
        category_stmt = select(
            BusinessRule.category,
            func.count(BusinessRule.id).label('count')
        ).group_by(BusinessRule.category)
        category_result = await db.execute(category_stmt)
        category_counts = {row[0]: row[1] for row in category_result.all()}
        return BusinessRuleListResponse(
            rules=rules,
            total=total,
            category_counts=category_counts
        )
    except Exception as e:
        logger.error(f"Failed to fetch rules: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch rules: {str(e)}"
        )


@router.get("/{rule_id}", response_model=BusinessRuleResponse)
async def get_business_rule(
    rule_id: str,
    db: AsyncSession = Depends(get_session)
):
    try:
        stmt = select(BusinessRule).where(
            or_(
                BusinessRule.id == rule_id,
                BusinessRule.rule_id == rule_id
            )
        )
        result = await db.execute(stmt)
        rule = result.scalars().first()
        if not rule:
            raise HTTPException(status_code=404, detail="Rule not found")
        promt_stmt = select(RulePrompt).where(
            RulePrompt.rule_id == rule.id).order_by(RulePrompt.priority.desc())
        prompt_result = await db.execute(promt_stmt)
        rule.prompts = prompt_result.scalars().all()
        return rule
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch rule: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch rule: {str(e)}"
        )


@router.put("/{rule_id}", response_model=BusinessRuleResponse)
async def update_business_rule(
    rule_id: str,
    updates: BusinessRuleUpdate,
    db: AsyncSession = Depends(get_session)
):
    try:
        stmt = select(BusinessRule).where(
            or_(
                BusinessRule.id == rule_id,
                BusinessRule.rule_id == rule_id
            )
        )
        result = await db.execute(stmt)
        rule = result.scalars().first()
        if not rule:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Rule '{rule_id}' not found"
            )
        if rule.is_system:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="System rules cannot be modified"
            )
        for field, value in updates.dict(exclude_unset=True).items():
            setattr(rule, field, value)
        if updates.title and updates.title != rule.title:
            new_rule_id = BusinessRule.generate_rule_id(updates.title)
            check_stmt = select(BusinessRule).where(
                BusinessRule.rule_id == new_rule_id, BusinessRule.id != rule_id)
            check_result = await db.execute(check_stmt)
            if not check_result.scalars().first():
                rule.rule_id = new_rule_id
        rule.updated_at = datetime.utcnow()
        db.add(rule)
        await db.commit()
        final_stmt = (
            select(BusinessRule)
            .options(selectinload(BusinessRule.prompts))
            .where(BusinessRule.id == rule.id)
        )
        final_result = await db.execute(final_stmt)
        updated_rule = final_result.scalars().one()
        logger.info(f"Updated business rule: {rule.rule_id}")
        return updated_rule
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to update rule: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update rule: {str(e)}"
        )

@router.patch('/prompts/{prompt_id}/status',response_model=RulePromptResponse)
async def update_prompt_status(prompt_id:str,new_status:RuleStatus,db:AsyncSession=Depends(get_session)):
    try:
        prompt=await db.get(RulePrompt,prompt_id)
        if not prompt:
            raise HTTPException(status_code=404,detail='Prompt not found')
        prompt.status=new_status
        prompt.updated_at=datetime.utcnow()
        db.add(prompt)
        stmt=(select(BusinessRule).options(selectinload(BusinessRule.prompts)).where(BusinessRule.id==prompt.rule_id))
        result=await db.execute(stmt)
        rule=result.scalars().one_or_none()
        if rule:
            all_prompts_inactive=all(p.status==RuleStatus.INACTIVE for p in rule.prompts)
            if all_prompts_inactive and rule.status==RuleStatus.ACTIVE:
                rule_status=RuleStatus.INACTIVE
                rule.updated_at=datetime.utcnow()
                db.add(rule)
                logger.info(f"Rule '{rule.rule_id}' auto_deactivated as all its prompts are inactive")
            elif not all_prompts_inactive and rule.status==RuleStatus.INACTIVE:
                rule.status=RuleStatus.ACTIVE
                rule.updated_at=datetime.utcnow()
                db.add(rule)
                logger.info(f"Rule '{rule.rule_id}' auto-activated as at least one prompt is active.")
        await db.commit()
        await db.refresh(prompt)
        logger.info(f"Updated prompt {prompt.id} status to {new_status}")
        return prompt
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to update prompt status :{str(e)}",exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update prompt status. {str(e)}"
        )

@router.put('/prompts/{prompt_id}', response_model=RulePromptResponse)
async def update_prompt(prompt_id: str, updates: RulePromptUpdate, db: AsyncSession = Depends(get_session)):
    try:
        prompt = await db.get(RulePrompt, prompt_id)
        if not prompt:
            raise HTTPException(status_code=404, detail='Prompt not found')
        for field, value in updates.dict(exclude_unset=True).items():
            setattr(prompt, field, value)
        prompt.updated_at = datetime.utcnow()
        db.add(prompt)
        await db.commit()
        await db.refresh(prompt)
        return prompt
    except Exception as e:
        logger.error(f"Failed to update prompt: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update prompt: {str(e)}"
        )


@router.get('/{rule_identifier}/prompts', response_model=List[RulePromptResponse])
async def get_prompts_for_rule(rule_identifier: str, db: AsyncSession = Depends(get_session)):
    try:
        stmt = select(BusinessRule).where(
            or_(BusinessRule.id == rule_identifier, BusinessRule.rule_id == rule_identifier))
        result = await db.execute(stmt)
        rule = result.scalars().first()
        if not rule:
            raise HTTPException(status_code=404, detail='Rule not found')
        prompt_stmt = select(RulePrompt).where(
            RulePrompt.rule_id == rule.id).order_by(RulePrompt.priority.desc())
        prompt_result = await db.execute(prompt_stmt)
        return prompt_result.scalars().all()
    except Exception as e:
        logger.error(
            f"Failed to get prompts for the rule: {str(rule_identifier)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get prompts for the rule: {str(e)}"
        )


@router.post('/{rule_identifier}/prompts', response_model=RulePromptResponse, status_code=status.HTTP_201_CREATED)
async def create_prompt_for_rule(rule_identifier: str, prompt: RulePromptCreate, db: AsyncSession = Depends(get_session)):
    try:
        stmt = select(BusinessRule).where(
            or_(BusinessRule.id == rule_identifier, BusinessRule.rule_id == rule_identifier))
        result = await db.execute(stmt)
        rule = result.scalars().first()
        if not rule:
            raise HTTPException(status_code=404, detail='Rule not found')
        new_prompt = RulePrompt(**prompt.dict(), rule_id=rule.id)
        db.add(new_prompt)
        await db.commit()
        await db.refresh(new_prompt)
        return new_prompt
    except Exception as e:
        raise e
