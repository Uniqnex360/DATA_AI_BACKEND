from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context
from sqlmodel import SQLModel
import sys
from pathlib import Path
from app.models.attribute import AttributeValue,Attribute,CategoryAttribute
from app.models.product_attribute_link import ProductAttributeLinkModel, ProductAttributeValueLinkModel
import alembic_autogenerate_enums

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings

from app.models.base import UUIDModel
from app.models.product import Product
from app.models.category import Category
from app.models.project import Project
from app.models.user import User
from app.models.pipeline import (
    AuditTrail,
    CleansingIssue,
    StandardizedAttribute,
    Source,
    ReviewItem,
    SourcePriority,
    Enrichment,
    RawExtraction,
    PublishTarget,
    AggregationJob 
)
from app.models.business_rule import BusinessRule,RulePrompt

config = context.config

db_url = settings.DATABASE_URL.replace(
    "postgresql+asyncpg://",
    "postgresql+psycopg2://"
)
config.set_main_option('sqlalchemy.url', db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        process_revision_directives=alembic_autogenerate_enums.add_alembic_autogenerate_enums,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()