import asyncio
import logging
from typing import Optional

from sqlmodel import select
from sqlalchemy.orm import Session

from app.core.database import async_session_factory  # adjust to your project
from app.models.category_alias_job import CategoryAliasJob
from app.services.canonical_alias_service import process_category_alias_resolution

logger = logging.getLogger("canonical_alias_worker")


async def start_canonical_worker(llm_provider: str = "openai", poll_interval_sec: int = 60) -> None:
    """Polls DB for pending CategoryAliasJob and runs them serially."""
    logger.info("Category Alias Worker started")

    while True:
        try:
            async with async_session_factory() as db:
                # Use SKIP LOCKED to be safe if multiple instances run.
                stmt = (
                    select(CategoryAliasJob)
                    .where(CategoryAliasJob.status == "pending")
                    .order_by(CategoryAliasJob.created_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                job = (await db.execute(stmt)).scalars().first()

                if not job:
                    await asyncio.sleep(poll_interval_sec)
                    continue

                job.status = "in_progress"
                job.attempts = (job.attempts or 0) + 1
                await db.commit()

                try:
                    await process_category_alias_resolution(
                        job.category_id, db, llm_provider
                    )
                    job.status = "completed"
                    job.last_error = None
                except Exception as e:
                    logger.exception(f"Alias job {job.id} failed: {e}")
                    job.status = "failed"
                    job.last_error = str(e)[:1000]
                finally:
                    await db.commit()
        except Exception as outer_e:
            logger.exception(f"Canonical worker loop error: {outer_e}")
            await asyncio.sleep(poll_interval_sec)