"""llm_calls.model: 128 -> 512 (строки-списки моделей не влезают)

Revision ID: 0039
Revises: 0038
"""
import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("llm_calls", "model",
                    existing_type=sa.String(128), type_=sa.String(512),
                    existing_nullable=False)


def downgrade() -> None:
    op.alter_column("llm_calls", "model",
                    existing_type=sa.String(512), type_=sa.String(128),
                    existing_nullable=False)