"""target_channels: убрать classify_providers (пиннинг переехал в строку classify_model),
расширить classify_model до 512

Revision ID: 0038
Revises: 0037
"""
import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"]: c for c in sa.inspect(bind).get_columns("target_channels")}
    if "classify_providers" in cols:
        op.drop_column("target_channels", "classify_providers")
    cm = cols.get("classify_model")
    if cm is not None and getattr(cm["type"], "length", None) != 512:
        op.alter_column("target_channels", "classify_model",
                        existing_type=cm["type"], type_=sa.String(512),
                        existing_nullable=True)


def downgrade() -> None:
    from sqlalchemy.dialects.postgresql import JSONB
    op.add_column("target_channels", sa.Column("classify_providers", JSONB, nullable=True))