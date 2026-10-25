"""llm_stage: значение editorial_media для vision-гейта медиа (M1).

Revision ID: 0050
Revises: 0049
"""
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE llm_stage ADD VALUE IF NOT EXISTS 'editorial_media'")


def downgrade() -> None:
    pass