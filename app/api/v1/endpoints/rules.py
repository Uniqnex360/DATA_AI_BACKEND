from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from app.core.database import get_session
from app.models.pipeline import BusinessRule 
from typing import Dict
import logging
logger = logging.getLogger("rules")
router = APIRouter()

@router.post("/seed")
async def seed_rules(payload: Dict, db: AsyncSession = Depends(get_session)):
    try:
        rules_data = payload.get("rules", [])
        
        for item in rules_data:
            statement = select(BusinessRule).where(BusinessRule.rule_id == item["rule_id"])
            result = await db.execute(statement)
            existing_rule = result.scalars().first()

            if existing_rule:
                existing_rule.attribute_name = item.get("attribute_name")
                existing_rule.rule_type = item.get("rule_type")
                existing_rule.rule_config = item.get("rule_config")
                existing_rule.active = item.get("active", True)
                db.add(existing_rule) 
            else:
                new_rule = BusinessRule(**item)
                db.add(new_rule)
        
        await db.commit()
        return {"msg": "Rules synchronized successfully"}

    except Exception as e:
        await db.rollback()
        logger.error(f"Seeding failed: {str(e)}")
        raise HTTPException(status_code=500, detail="Database sync failed")
@router.get("/")
async def get_rules(db: AsyncSession = Depends(get_session)):
    try:
        statement = select(BusinessRule).order_by(BusinessRule.created_at.desc())
        result = await db.execute(statement)
        return result.scalars().all()
    except Exception as e:
        logger.error(f"Failed to fetch rules: {e}")
        return []