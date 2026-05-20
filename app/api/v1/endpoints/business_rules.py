from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, func, and_, or_
from typing import List, Optional
from datetime import datetime, timedelta, timezone
from app.core.database import get_session
from app.models.business_rule import BrandPrompt, BusinessRule, CategoryPrompt, RuleCategory, RulePrompt, RuleStatus
from app.models.category import Category
from app.schemas.business_rule import (BrandPromptCreate, BrandPromptResponse, BusinessRuleCreate, BusinessRuleUpdate, BusinessRuleResponse,BusinessRuleListResponse, CategoryPromptCreate, CategoryPromptResponse, RulePromptResponse, RulePromptCreate, RulePromptUpdate,)
import logging
from sqlalchemy.orm import selectinload
from app.utils.timezone import now_ist
from app.models.brand import Brand
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
            if not existing.scalars().first():
                break 
            counter += 1
            rule_id = f"{base_rule_id}_{counter}"
        new_rule = BusinessRule(
            **rule.dict(),
            rule_id=rule_id,  
            created_at=now_ist(),
            updated_at=now_ist()
        )
        db.add(new_rule)
        await db.commit()
        stmt = select(BusinessRule).options(selectinload(BusinessRule.prompts)).where(BusinessRule.id == new_rule.id)
        result = await db.execute(stmt)
        created_rule_with_prompts = result.scalars().one()
        logger.info(f"Created business rule: {created_rule_with_prompts.rule_id}")   
        return created_rule_with_prompts
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to create rule: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred while creating the rule."
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
            search_term = search.lower()
            conditions.append(
                or_(
                    func.lower(BusinessRule.title).contains(search_term),
                    func.lower(BusinessRule.description).contains(search_term),
                    BusinessRule.prompts.any(
                        or_(
                            func.lower(RulePrompt.prompt_name).contains(search_term),
                            func.lower(RulePrompt.prompt_text).contains(search_term)
                        )
                    )
                )
            )
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(BusinessRule.created_at.desc())
        result = await db.execute(stmt)
        rules = result.scalars().unique().all()
        total = len(rules) 
        category_counts = {}
        all_rules_result = await db.execute(select(BusinessRule.category))
        all_categories = all_rules_result.scalars().all()
        for cat in all_categories:
            category_counts[cat] = category_counts.get(cat, 0) + 1
        return BusinessRuleListResponse(
            rules=rules,
            total=total,
            category_counts=category_counts
        )
    except Exception as e:
        logger.error(f"Failed to fetch rules: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to fetch rules."
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
        rule.updated_at = now_ist()()
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
        prompt.updated_at=now_ist()()
        db.add(prompt)
        stmt=(select(BusinessRule).options(selectinload(BusinessRule.prompts)).where(BusinessRule.id==prompt.rule_id))
        result=await db.execute(stmt)
        rule=result.scalars().one_or_none()
        if rule:
            all_prompts_inactive=all(p.status==RuleStatus.INACTIVE for p in rule.prompts)
            if all_prompts_inactive and rule.status==RuleStatus.ACTIVE:
                rule_status=RuleStatus.INACTIVE
                rule.updated_at=now_ist()()
                db.add(rule)
                logger.info(f"Rule '{rule.rule_id}' auto_deactivated as all its prompts are inactive")
            elif not all_prompts_inactive and rule.status==RuleStatus.INACTIVE:
                rule.status=RuleStatus.ACTIVE
                rule.updated_at=now_ist()()
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
        prompt.updated_at = now_ist()()
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
@router.patch('/{rule_id}/status', response_model=BusinessRuleResponse)
async def update_rule_status(
    rule_id: str,
    new_status: RuleStatus,
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
        if new_status == RuleStatus.INACTIVE:
            prompt_stmt = select(RulePrompt).where(RulePrompt.rule_id == rule.id)
            prompt_result = await db.execute(prompt_stmt)
            prompts = prompt_result.scalars().all()
            for prompt in prompts:
                prompt.status = RuleStatus.INACTIVE
                prompt.updated_at = now_ist()()
                db.add(prompt)
        rule.status = new_status
        rule.updated_at = now_ist()()
        db.add(rule)
        await db.commit()
        final_stmt = (
            select(BusinessRule)
            .options(selectinload(BusinessRule.prompts))
            .where(BusinessRule.id == rule.id)
        )
        final_result = await db.execute(final_stmt)
        updated_rule = final_result.scalars().one()
        return updated_rule
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to update rule status: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update rule status"
        )
@router.delete('/prompts/{prompt_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_prompt(
    prompt_id: str,
    db: AsyncSession = Depends(get_session)
):
    try:
        prompt = await db.get(RulePrompt, prompt_id)
        if not prompt:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Prompt not found'
            )
        rule_id = prompt.rule_id
        await db.delete(prompt)
        stmt = (
            select(BusinessRule)
            .options(selectinload(BusinessRule.prompts))
            .where(BusinessRule.id == rule_id)
        )
        result = await db.execute(stmt)
        rule = result.scalars().one_or_none()
        if rule:
            active_prompts = [p for p in rule.prompts if p.status == RuleStatus.ACTIVE]
            if not active_prompts and rule.status == RuleStatus.ACTIVE:
                rule.status = RuleStatus.INACTIVE
                rule.updated_at = now_ist()()
                db.add(rule)
        await db.commit()
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to delete prompt: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete prompt"
        )
@router.delete('/{rule_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_business_rule(
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
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Rule '{rule_id}' not found"
            )
        if rule.is_system:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="System rules cannot be deleted"
            )
        await db.delete(rule)
        await db.commit()
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to delete rule: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete rule"
        )
        
# @router.patch("/prompts/{prompt_id}/status")
# async def update_prompt_status(
#     prompt_id: str,
#     new_status: RuleStatus,
#     db: AsyncSession = Depends(get_session)
# ):
#     try:
#         prompt = await db.get(RulePrompt, prompt_id)
#         if not prompt:
#             raise HTTPException(status_code=404, detail="Prompt not found")
#         rule = await db.get(BusinessRule, prompt.rule_id)
#         if not rule:
#             raise HTTPException(status_code=404, detail="Rule not found")
#         if new_status == RuleStatus.ACTIVE:
#             stmt = (
#                 select(RulePrompt)
#                 .join(BusinessRule, RulePrompt.rule_id == BusinessRule.id)
#                 .where(
#                     RulePrompt.stage == prompt.stage,
#                     BusinessRule.operation_mode == rule.operation_mode,
#                     BusinessRule.use_case == rule.use_case,
#                     RulePrompt.id != prompt.id,
#                     RulePrompt.status == RuleStatus.ACTIVE,
#                 )
#             )
#             result = await db.execute(stmt)
#             others = result.scalars().all()
#             for p in others:
#                 p.status = RuleStatus.INACTIVE
#                 p.updated_at = now_ist()()
#                 db.add(p)
#         prompt.status = new_status
#         prompt.updated_at = now_ist()()
#         db.add(prompt)
#         await db.commit()
#         await db.refresh(prompt)
#         return prompt
#     except HTTPException:
#         raise
#     except Exception as e:
#         await db.rollback()
#         print(f"Error updating prompt status for {prompt_id}: {e}")
#         raise HTTPException(status_code=500, detail="Internal server error")

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

@router.get("/category-prompts", response_model=List[CategoryPromptResponse])
async def get_category_prompts(
    category_id: Optional[str] = None,
    stage: Optional[str] = None,
    db: AsyncSession = Depends(get_session)
):
    try:
        stmt = select(CategoryPrompt, Category.name).outerjoin(
            Category, CategoryPrompt.category_id == Category.id
        )
        
        if category_id:
            stmt = stmt.where(CategoryPrompt.category_id == category_id)
        if stage:
            stmt = stmt.where(CategoryPrompt.stage == stage)

        result = await db.execute(stmt)
        rows = result.all()

        response = []
        for prompt, cat_name in rows:
            prompt_dict = prompt.dict()
            prompt_dict['category_name'] = cat_name  
            response.append(CategoryPromptResponse(**prompt_dict))

        return response

    except SQLAlchemyError as e:
        logger.error(f"Database error: {e}")
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.error(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/category-prompts", response_model=CategoryPromptResponse, status_code=201)
async def create_category_prompt(
    payload: CategoryPromptCreate,
    db: AsyncSession = Depends(get_session)
):
    try:
        if not payload.category_id and payload.selected_taxonomy:
            parts = [p.strip().lower() for p in payload.selected_taxonomy.split(">")]
            stmt = select(Category.id).where(
                func.lower(Category.name).in_(parts)
            ).order_by(Category.level.desc())
            result = await db.execute(stmt)
            cat_id = result.scalar_one_or_none()
            
            if cat_id:
                payload.category_id = cat_id
            else:
                raise HTTPException(400, f"Category not found: {payload.selected_taxonomy}")
        
        if not payload.category_id:
            raise HTTPException(400, "category_id or selected_taxonomy required")
        
        prompt = CategoryPrompt(
            **payload.dict(),
            created_at=now_ist()(), 
            updated_at=now_ist()()   
        )
        db.add(prompt)
        await db.commit()
        await db.refresh(prompt)

        cat = await db.get(Category, prompt.category_id)

        return CategoryPromptResponse(
            **prompt.dict(),
            category_name=cat.name if cat else None
        )

    except HTTPException:
        raise
    except SQLAlchemyError as e:
        await db.rollback()
        logger.error(f"SQLAlchemy error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    except Exception as e:
        await db.rollback()
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/category-prompts/{prompt_id}", response_model=CategoryPromptResponse)
async def update_category_prompt(
    prompt_id: str,
    payload: CategoryPromptCreate,
    db: AsyncSession = Depends(get_session)
):
    try:
        prompt = await db.get(CategoryPrompt, prompt_id)
        if not prompt:
            raise HTTPException(404, "Prompt not found")

        for k, v in payload.dict(exclude_unset=True).items():
            setattr(prompt, k, v)

        prompt.updated_at = now_ist()

        db.add(prompt)
        await db.commit()
        await db.refresh(prompt)

        cat = await db.get(Category, prompt.category_id)

        return CategoryPromptResponse(
            **prompt.dict(),
            category_name=cat.name if cat else None
        )

    except HTTPException:
        raise
    except SQLAlchemyError:
        await db.rollback()
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/category-prompts/{prompt_id}", status_code=204)
async def delete_category_prompt(prompt_id: str, db: AsyncSession = Depends(get_session)):
    try:
        prompt = await db.get(CategoryPrompt, prompt_id)
        if not prompt:
            raise HTTPException(404, "Prompt not found")

        await db.delete(prompt)
        await db.commit()

    except HTTPException:
        raise
    except SQLAlchemyError:
        await db.rollback()
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# ── Brand Prompts ──

@router.get("/brand-prompts", response_model=List[BrandPromptResponse])
async def get_brand_prompts(
    brand_id: Optional[str] = None,
    stage: Optional[str] = None,
    db: AsyncSession = Depends(get_session)
):
    try:
        stmt = select(BrandPrompt, Brand.name).join(
            Brand, BrandPrompt.brand_id == Brand.id
        )

        if brand_id:
            stmt = stmt.where(BrandPrompt.brand_id == brand_id)
        if stage:
            stmt = stmt.where(BrandPrompt.stage == stage)

        result = await db.execute(stmt)
        rows = result.all()

        return [
            BrandPromptResponse(**prompt.dict(), brand_name=brand_name)
            for prompt, brand_name in rows
        ]

    except SQLAlchemyError:
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/brand-prompts", response_model=BrandPromptResponse, status_code=201)
async def create_brand_prompt(
    payload: BrandPromptCreate,
    db: AsyncSession = Depends(get_session)
):
    try:
        prompt = BrandPrompt(
            **payload.dict(),
            created_at=now_ist(),
            updated_at=now_ist()
        )

        db.add(prompt)
        await db.commit()
        await db.refresh(prompt)

        brand = await db.get(Brand, prompt.brand_id)

        return BrandPromptResponse(
            **prompt.dict(),
            brand_name=brand.name if brand else None
        )

    except SQLAlchemyError as e:
        await db.rollback()
        logger.exception("Database error in create_brand_prompt")
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    except Exception as e:
        await db.rollback()
        logger.exception("Unexpected error in create_brand_prompt")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/brand-prompts/{prompt_id}", response_model=BrandPromptResponse)
async def update_brand_prompt(
    prompt_id: str,
    payload: BrandPromptCreate,
    db: AsyncSession = Depends(get_session)
):
    try:
        prompt = await db.get(BrandPrompt, prompt_id)
        if not prompt:
            raise HTTPException(404, "Prompt not found")

        for k, v in payload.dict(exclude_unset=True).items():
            setattr(prompt, k, v)

        prompt.updated_at = now_ist()

        db.add(prompt)
        await db.commit()
        await db.refresh(prompt)

        brand = await db.get(Brand, prompt.brand_id)

        return BrandPromptResponse(
            **prompt.dict(),
            brand_name=brand.name if brand else None
        )

    except HTTPException:
        raise
    except SQLAlchemyError:
        await db.rollback()
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/brand-prompts/{prompt_id}", status_code=204)
async def delete_brand_prompt(prompt_id: str, db: AsyncSession = Depends(get_session)):
    try:
        prompt = await db.get(BrandPrompt, prompt_id)
        if not prompt:
            raise HTTPException(404, "Prompt not found")

        await db.delete(prompt)
        await db.commit()

    except HTTPException:
        raise
    except SQLAlchemyError:
        await db.rollback()
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))    
    

        
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