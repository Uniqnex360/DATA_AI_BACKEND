
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '19f74ae5a575'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('aggregation_jobs', sa.Column('project_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False))
    op.add_column('aggregation_jobs', sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False))
    op.add_column('aggregation_jobs', sa.Column('total_products', sa.Integer(), nullable=False))
    op.add_column('aggregation_jobs', sa.Column('successful', sa.Integer(), nullable=False))
    op.add_column('aggregation_jobs', sa.Column('failed', sa.Integer(), nullable=False))
    op.add_column('aggregation_jobs', sa.Column('current_product', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('aggregation_jobs', sa.Column('error_message', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('aggregation_jobs', sa.Column('started_at', sa.DateTime(), nullable=True))
    op.add_column('aggregation_jobs', sa.Column('completed_at', sa.DateTime(), nullable=True))
    op.add_column('aggregation_jobs', sa.Column('details', sa.JSON(), nullable=True))
    op.create_index(op.f('ix_aggregation_jobs_project_id'), 'aggregation_jobs', ['project_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_aggregation_jobs_project_id'), table_name='aggregation_jobs')
    op.drop_column('aggregation_jobs', 'details')
    op.drop_column('aggregation_jobs', 'completed_at')
    op.drop_column('aggregation_jobs', 'started_at')
    op.drop_column('aggregation_jobs', 'error_message')
    op.drop_column('aggregation_jobs', 'current_product')
    op.drop_column('aggregation_jobs', 'failed')
    op.drop_column('aggregation_jobs', 'successful')
    op.drop_column('aggregation_jobs', 'total_products')
    op.drop_column('aggregation_jobs', 'status')
    op.drop_column('aggregation_jobs', 'project_id')
