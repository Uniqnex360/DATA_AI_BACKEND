
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, func, and_, or_
from typing import List, Optional
from datetime import datetime

from app.core.database import get_session
from app.models.business_rule import BusinessRule, RuleExecutionLog, RuleCategory, RuleStatus
from app.schemas.business_rule import (
    BusinessRuleCreate,
    BusinessRuleUpdate,
    BusinessRuleResponse,
    BusinessRuleListResponse,
    RuleExecuteRequest,
    RuleExecuteResponse
)
from app.sacred import safe_call_llm

import logging

logger = logging.getLogger("business_rules")
router = APIRouter()

@router.post("/", response_model=BusinessRuleResponse, status_code=status.HTTP_201_CREATED)
async def create_business_rule(
    rule: BusinessRuleCreate,
    db: AsyncSession = Depends(get_session)
):
    try:
        stmt = select(BusinessRule).where(BusinessRule.rule_id == rule.rule_id)
        existing = await db.execute(stmt)
        if existing.scalars().first():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Rule with ID '{rule.rule_id}' already exists"
            )
        
        new_rule = BusinessRule(
            **rule.dict(),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        
        db.add(new_rule)
        await db.commit()
        await db.refresh(new_rule)
        
        logger.info(f"Created business rule: {new_rule.rule_id}")
        return new_rule
        
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
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_session)
):
    try:
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
                    BusinessRule.rule_id.ilike(search_pattern)
                )
            )
        
        stmt = select(BusinessRule)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        
        stmt = stmt.order_by(BusinessRule.priority.desc(), BusinessRule.created_at.desc())
        stmt = stmt.offset(skip).limit(limit)
        
        result = await db.execute(stmt)
        rules = result.scalars().all()
        
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
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Rule '{rule_id}' not found"
            )
        
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
        
        update_data = updates.dict(exclude_unset=True)
        for field, value in update_data.items():
            setattr(rule, field, value)
        
        rule.updated_at = datetime.utcnow()
        
        db.add(rule)
        await db.commit()
        await db.refresh(rule)
        
        logger.info(f"Updated business rule: {rule.rule_id}")
        return rule
        
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to update rule: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update rule: {str(e)}"
        )

@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
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
        
        logger.info(f" Deleted business rule: {rule.rule_id}")
        return
        
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to delete rule: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete rule: {str(e)}"
        )

@router.post("/{rule_id}/execute", response_model=RuleExecuteResponse)
async def execute_business_rule(
    rule_id: str,
    request: RuleExecuteRequest,
    db: AsyncSession = Depends(get_session)
):
    start_time = datetime.utcnow()
    
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
        
        if rule.status != RuleStatus.ACTIVE:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Rule '{rule_id}' is not active"
            )
        
        output = await safe_call_llm(rule, request.context)
        
        end_time = datetime.utcnow()
        execution_time_ms = int((end_time - start_time).total_seconds() * 1000)
        
        rule.execution_count += 1
        rule.last_executed_at = end_time
        db.add(rule)
        
        log = RuleExecutionLog(
            rule_id=rule.id,
            product_id=request.context.get('product_id'),
            input_data=request.context,
            output_data=output,
            status="success",
            execution_time_ms=execution_time_ms,
            executed_at=end_time
        )
        db.add(log)
        
        await db.commit()
        
        return RuleExecuteResponse(
            rule_id=rule.rule_id,
            status="success",
            output=output,
            execution_time_ms=execution_time_ms,
            executed_at=end_time
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Rule execution failed: {str(e)}")
        
        end_time = datetime.utcnow()
        execution_time_ms = int((end_time - start_time).total_seconds() * 1000)
        
        log = RuleExecutionLog(
            rule_id=rule.id if rule else None,
            input_data=request.context,
            status="failed",
            error_message=str(e),
            execution_time_ms=execution_time_ms,
            executed_at=end_time
        )
        db.add(log)
        await db.commit()
        
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rule execution failed: {str(e)}"
        )

@router.get("/{rule_id}/logs")
async def get_rule_execution_logs(
    rule_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_session)
):
    try:
        rule_stmt = select(BusinessRule).where(
            or_(
                BusinessRule.id == rule_id,
                BusinessRule.rule_id == rule_id
            )
        )
        rule_result = await db.execute(rule_stmt)
        rule = rule_result.scalars().first()
        
        if not rule:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Rule '{rule_id}' not found"
            )
        
        stmt = select(RuleExecutionLog).where(
            RuleExecutionLog.rule_id == rule.id
        ).order_by(RuleExecutionLog.executed_at.desc())
        stmt = stmt.offset(skip).limit(limit)
        
        result = await db.execute(stmt)
        logs = result.scalars().all()
        
        count_stmt = select(func.count(RuleExecutionLog.id)).where(
            RuleExecutionLog.rule_id == rule.id
        )
        total_result = await db.execute(count_stmt)
        total = total_result.scalar_one()
        
        return {
            "logs": logs,
            "total": total,
            "skip": skip,
            "limit": limit
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch logs: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch logs: {str(e)}"
        )