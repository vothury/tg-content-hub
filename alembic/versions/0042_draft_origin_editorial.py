"""draft_origin: значение editorial для черновиков виртуальной редакции.

Revision ID: 0042
Revises: 0041
"""
from __future__ import annotations

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE draft_origin ADD VALUE IF NOT EXISTS 'editorial'")


def downgrade() -> None:
    pass  # postgres не удаляет значения enum; значение останется неиспользуемым