from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from app.models.cleaning import CleaningTask
from sqlalchemy.ext.asyncio import AsyncSession
import logging
from app.utils.timezone import now_ist
logger=logging.getLogger('cleaning helper')

async def create_cleaning_task(db: AsyncSession, task_id: str) -> CleaningTask:
    try:
        task = CleaningTask(
            id=task_id,
            status="pending",
            logs=[],
            error=None,
            created_at=now_ist(),
            updated_at=now_ist(),
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        return task
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to create cleaning task {task_id}: {e}", exc_info=True)
        raise


async def get_cleaning_task_or_404(db: AsyncSession, task_id: str) -> CleaningTask:
    try:
        task = await db.get(CleaningTask, task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        return task
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch cleaning task {task_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch task")


async def update_cleaning_task_status(
    db: AsyncSession,
    task_id: str,
    status: str,
    error: str | None = None,
) -> None:
    try:
        task = await db.get(CleaningTask, task_id)
        if not task:
            logger.warning(f"Tried to update missing task {task_id}")
            return

        task.status = status
        task.error = error
        task.updated_at = now_ist()

        db.add(task)
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to update task {task_id} status: {e}", exc_info=True)
        raise


async def append_cleaning_task_log(
    db: AsyncSession,
    task_id: str,
    message: str,
) -> None:
    try:
        task = await db.get(CleaningTask, task_id)
        if not task:
            logger.warning(f"Tried to append log to missing task {task_id}")
            return

        timestamp = now_ist().isoformat()
        logs = list(task.logs or [])
        logs.append(f"{timestamp} - {message}")
        task.logs = logs
        task.updated_at = now_ist()

        db.add(task)
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to append log to task {task_id}: {e}", exc_info=True)
        raise