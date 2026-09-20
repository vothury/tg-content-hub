"""llm_stage: dedup_confirm

Revision ID: 0028
Revises: 0027
"""
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE llm_stage ADD VALUE IF NOT EXISTS 'dedup_confirm'")


def downgrade() -> None:
    pass  # значения enum в PostgreSQL не удаляются