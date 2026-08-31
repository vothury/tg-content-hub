"""telegram_id целевого канала может быть пустым до резолва ботом

Целевые каналы создаются декларативно из sources.yaml по юзернейму;
числовой telegram_id определяется ботом через Bot API в Этапе 5.

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("target_channels", "telegram_id",
                    existing_type=sa.BigInteger(), nullable=True)


def downgrade() -> None:
    op.alter_column("target_channels", "telegram_id",
                    existing_type=sa.BigInteger(), nullable=False)