
import asyncio
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.runtime.environment import EnvironmentContext
from sqlalchemy.ext.asyncio import create_async_engine
import logging

from app.core.config import settings

logger = logging.getLogger("smart_migration")

def get_alembic_config():
    return Config("alembic.ini")

async def run_migrations():
    
    alembic_cfg = get_alembic_config()
    
    
    sync_db_url = str(settings.DATABASE_URL).replace("postgresql+asyncpg", "postgresql")
    engine = create_async_engine(sync_db_url).sync_engine

    script = ScriptDirectory.from_config(alembic_cfg)
    
    def run_upgrade(revision, context):
        return script._upgrade_revs("head", revision)

    with EnvironmentContext(
        alembic_cfg,
        script=script,
        fn=run_upgrade,
        as_sql=False,
        starting_rev=None,
        destination_rev="head",
    ) as context:
        
        with engine.connect() as connection:
            context.configure(connection=connection)
            
            
            db_version = context.get_current_revision()
            head_version = script.get_current_head()

            if db_version == head_version:
                logger.info("Database is already up to date.")
                return

            logger.info(f"Database version is {db_version}, head is {head_version}. Upgrading...")
            
            context.run_migrations()
            logger.info("Database upgrade complete.")

async def smart_init_db():
    logger.info("Running smart database initialization...")
    try:
        
        await run_migrations()
        logger.info("Smart database initialization complete.")
    except Exception as e:
        logger.error(f"FATAL: Smart DB initialization failed: {e}", exc_info=True)