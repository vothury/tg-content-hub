"""editorial_web_sources: feed_url + css selectors

Revision ID: 0022
Revises: 0021
"""
from alembic import op
import sqlalchemy as sa

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("editorial_web_sources", sa.Column("feed_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("editorial_web_sources", "feed_url")