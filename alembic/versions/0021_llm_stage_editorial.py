"""llm_stage: editorial stages

Revision ID: 0021
Revises: 0020
"""
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for v in ("editorial_journalist", "editorial_chief",
                  "editorial_gather", "editorial_write"):
            op.execute(f"ALTER TYPE llm_stage ADD VALUE IF NOT EXISTS '{v}'")


def downgrade() -> None:
    pass  # PostgreSQL не умеет удалять значения enum