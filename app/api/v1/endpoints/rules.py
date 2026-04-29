from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from app.core.database import get_session
from typing import Dict
import logging
from app.models.business_rule import BusinessRule
logger = logging.getLogger("rules")
router = APIRouter()
from sqlalchemy.dialects.postgresql import insert 
from datetime import datetime, timedelta, timezone
from app.utils.timezone import now_ist

@router.post("/seed")
async def seed_rules(payload: Dict, db: AsyncSession = Depends(get_session)):
    try:
        rules_data = payload.get("rules", [])
        
        for item in rules_data:
            rule_id = item.get("rule_id")
            if not rule_id:
                logger.warning(f"Skipping rule item missing 'rule_id': {item}")
            title = item.get("title", item.get("rule_id", "").replace("_", " ").title())
            category = item.get("category", "enrichment")
            stmt = insert(BusinessRule).values(
                rule_id=item["rule_id"],
                title=title,
                category=category,
                status='active' if item.get("active", True) else 'inactive',
                updated_at=now_ist()
            )

            stmt = stmt.on_conflict_do_update(
                index_elements=['rule_id'],
                set_={
                    "title": title,
                    "category": category,
                    "status": 'active' if item.get("active", True) else 'inactive',
                    "updated_at": now_ist()
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
        raise HTTPException(status_code=500, detail="Failed to fetch rules")