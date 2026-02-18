from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from app.core.database import get_session
from app.models.pipeline import BusinessRule 
from typing import Dict
import logging
logger = logging.getLogger("rules")
router = APIRouter()
from sqlalchemy.dialects.postgresql import insert 
from app.models.pipeline import BusinessRule
from datetime import datetime

@router.post("/seed")
async def seed_rules(payload: Dict, db: AsyncSession = Depends(get_session)):
    try:
        rules_data = payload.get("rules", [])
        
        for item in rules_data:
            stmt = insert(BusinessRule).values(
                rule_id=item["rule_id"],
                attribute_name=item["attribute_name"],
                rule_type=item["rule_type"],
                rule_config=item["rule_config"],
                active=item.get("active", True),
                updated_at=datetime.utcnow()
            )

            stmt = stmt.on_conflict_do_update(
                index_elements=['rule_id'],
                set_={
                    "attribute_name": item["attribute_name"],
                    "rule_type": item["rule_type"],
                    "rule_config": item["rule_config"],
                    "active": item.get("active", True),
                    "updated_at": datetime.utcnow()
                }
            )
            
            await db.execute(stmt)
        
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