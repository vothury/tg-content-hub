"""llm_stage: значение editorial_gate для гейта Editor-in-Chief (B1).

Revision ID: 0049
Revises: 0048
"""
from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE llm_stage ADD VALUE IF NOT EXISTS 'editorial_gate'")


def downgrade() -> None:
    pass  # postgres не умеет удалять значения enum