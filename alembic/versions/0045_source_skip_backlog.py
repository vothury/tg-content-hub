"""sources: skip_backlog - одноразовый прыжок курсора на свежий пост по кнопке UI.

Revision ID: 0045
Revises: 0044
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sources",
                  sa.Column("skip_backlog", sa.Boolean(),
                            nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("sources", "skip_backlog")