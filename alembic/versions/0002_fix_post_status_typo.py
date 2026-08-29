"""fix post_status enum typo: UNSUATABLE -> UNSUITABLE

Опечатка в миграции 0001: значение перечисления создано как 'UNSUATABLE',
а код использует 'UNSUITABLE'. Переименовываем значение.
Строк со старым значением в таблице нет (ни один коммит не проходил).

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-29
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE post_status RENAME VALUE 'UNSUATABLE' TO 'UNSUITABLE'")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE post_status RENAME VALUE 'UNSUITABLE' TO 'UNSUATABLE'")