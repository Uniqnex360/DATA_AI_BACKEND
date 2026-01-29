import logging
from sqlalchemy.ext.asyncio import AsyncSession,create_async_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import settings
from sqlmodel import SQLModel
logger=logging.getLogger(__name__)
engine=create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DB_ECHO_LOG,
    future=True,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_pre_ping=True
)
async_session_factory=sessionmaker(bind=engine,class_=AsyncSession,expire_on_commit=False,autoflush=False)
async def get_session()->AsyncSession:
    async with async_session_factory() as session:
        try:
            yield session
        except Exception as e:
            logger.error(f"DB Session Error:{e}")
            await session.rollback()
            raise
        finally:
            await session.close()
async def init_db():
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        logger.info(" Database schema restored successfully.")
    except Exception as e:
        logger.error(f" Failed to restore database: {e}")