"""sources: per-source llm_instructions for classify prompt

Revision ID: 0025
Revises: 0024
"""
from alembic import op
import sqlalchemy as sa

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("llm_instructions", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("sources", "llm_instructions")