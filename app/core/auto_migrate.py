
import logging
from sqlalchemy import text, inspect, MetaData, Table, Column
from sqlalchemy.schema import CreateTable
from sqlmodel import SQLModel
from typing import Dict, List, Set, Tuple

logger = logging.getLogger(__name__)


class AutoMigration:
    
    def __init__(self):
        self.metadata = SQLModel.metadata
        
    async def run(self, mode: str = "safe"):
        from app.core.database import engine
        
        logger.info(f" Running auto-migration in '{mode}' mode...")
        
        
        self._import_all_models()
        
        if mode == "reset":
            await self._reset_database()
        elif mode == "sync":
            await self._sync_database()
        else:  
            await self._safe_migration()
        
        logger.info(" Auto-migration completed successfully")
    
    def _import_all_models(self):
        from app.models.base import UUIDModel
        from app.models.product import Product
        from app.models.project import Project
        from app.models.user import User
        from app.models.pipeline import (
            AuditTrail, CleansingIssue, StandardizedAttribute,
            BusinessRule, Source, ReviewItem, SourcePriority,
            Enrichment, RawExtraction, PublishTarget,
            AggregationJob
        )
        logger.info(f" Loaded {len(self.metadata.tables)} table definitions")
    
    async def _safe_migration(self):
        from app.core.database import engine
        async with engine.begin() as conn:
            
            existing_tables = await self._get_existing_tables(conn)
            expected_tables = set(self.metadata.tables.keys())
            
            
            missing_tables = expected_tables - existing_tables
            
            if missing_tables:
                logger.info(f" Creating {len(missing_tables)} new tables: {missing_tables}")
                
                await conn.run_sync(
                    lambda sync_conn: self.metadata.create_all(
                        sync_conn,
                        tables=[self.metadata.tables[t] for t in missing_tables]
                    )
                )
                logger.info(f" Created {len(missing_tables)} tables")
            else:
                logger.info(" All tables exist")
            
            
            await self._add_missing_columns(conn, existing_tables)
            
            
            await self._create_missing_indexes(conn)
    
    async def _sync_database(self):
        from app.core.database import engine
        logger.warning("  Sync mode: This may remove data!")
        async with engine.begin() as conn:
            
            await conn.run_sync(self.metadata.drop_all)
            await conn.run_sync(self.metadata.create_all)
        logger.info(" Database synced with models")
    
    async def _reset_database(self):
        from app.core.database import engine
        logger.warning(" RESET MODE: ALL DATA WILL BE LOST!")
        async with engine.begin() as conn:
            await conn.run_sync(self.metadata.drop_all)
            await conn.run_sync(self.metadata.create_all)
        logger.info(" Database reset complete")
    
    async def _get_existing_tables(self, conn) -> Set[str]:
        def get_tables(sync_conn):
            inspector = inspect(sync_conn)
            return set(inspector.get_table_names())
        
        return await conn.run_sync(get_tables)
    
    async def _add_missing_columns(self, conn, existing_tables: Set[str]):
        def get_columns(sync_conn, table_name):
            inspector = inspect(sync_conn)
            try:
                return {col['name']: col for col in inspector.get_columns(table_name)}
            except Exception:
                return {}
        
        for table_name in existing_tables:
            if table_name not in self.metadata.tables:
                continue
            
            
            existing_columns = await conn.run_sync(
                lambda sync_conn: get_columns(sync_conn, table_name)
            )
            
            
            model_table = self.metadata.tables[table_name]
            expected_columns = {col.name: col for col in model_table.columns}
            
            
            missing_columns = set(expected_columns.keys()) - set(existing_columns.keys())
            
            if missing_columns:
                logger.info(f" Adding {len(missing_columns)} columns to {table_name}: {missing_columns}")
                
                for col_name in missing_columns:
                    col = expected_columns[col_name]
                    await self._add_column(conn, table_name, col)
    
    async def _add_column(self, conn, table_name: str, column: Column):
        try:
            
            col_type = column.type.compile(conn.dialect)
            nullable = "NULL" if column.nullable else "NOT NULL"
            
            
            default = ""
            if column.server_default is not None:
                default = f"DEFAULT {column.server_default.arg}"
            elif not column.nullable:
                
                if 'VARCHAR' in str(col_type) or 'TEXT' in str(col_type):
                    default = "DEFAULT ''"
                elif 'INTEGER' in str(col_type):
                    default = "DEFAULT 0"
                elif 'BOOLEAN' in str(col_type):
                    default = "DEFAULT FALSE"
                elif 'TIMESTAMP' in str(col_type) or 'DATE' in str(col_type):
                    default = ""  
                    nullable = "NULL"
                elif 'JSON' in str(col_type):
                    default = "DEFAULT '{}'"
            
            
            sql = text(
                f"ALTER TABLE {table_name} "
                f"ADD COLUMN IF NOT EXISTS {column.name} {col_type} {nullable} {default}"
            )
            
            await conn.execute(sql)
            logger.info(f"   Added column {table_name}.{column.name}")
            
        except Exception as e:
            logger.warning(f"    Failed to add {table_name}.{column.name}: {e}")
    
    async def _create_missing_indexes(self, conn):
        
        indexes = [
            ("aggregation_jobs", "project_id", False),
            ("aggregation_jobs", "status", False),
            ("product_master", "project_id", False),
            ("product_master", "product_code", True),  
            ("sources", "project_id", False),
            ("raw_extractions", "source_id", False),
        ]
        
        for table_name, column_name, unique in indexes:
            try:
                index_name = f"idx_{table_name}_{column_name}"
                unique_clause = "UNIQUE" if unique else ""
                
                sql = text(
                    f"CREATE {unique_clause} INDEX IF NOT EXISTS {index_name} "
                    f"ON {table_name}({column_name})"
                )
                
                await conn.execute(sql)
                logger.info(f"   Created index {index_name}")
                
            except Exception as e:
                logger.debug(f"    Index {index_name} might already exist: {e}")



auto_migration = AutoMigration()