"""media: keep original file name after purge

Revision ID: 0016
Revises: 0015
"""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("media_items", sa.Column("original_name", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("media_items", "original_name")