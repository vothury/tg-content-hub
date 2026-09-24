"""curation: posts.curated + sources.manual

Revision ID: 0034
Revises: 0033
"""
from alembic import op
import sqlalchemy as sa

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("posts", sa.Column("curated", sa.Boolean(),
                                     nullable=False, server_default=sa.false()))
    op.add_column("sources", sa.Column("manual", sa.Boolean(),
                                       nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("sources", "manual")
    op.drop_column("posts", "curated")