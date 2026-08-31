from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from app.db.models import TargetChannel
from app.db.session import session_scope


def review_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"p:{post_id}:approve"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"p:{post_id}:reject"),
        ],
        [
            InlineKeyboardButton(text="🤖 Правка ИИ", callback_data=f"p:{post_id}:ai"),
            InlineKeyboardButton(text="✏️ Редактор", callback_data=f"p:{post_id}:edit"),
        ],
    ])


def media_review_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подходит (в рерайт)", callback_data=f"p:{post_id}:media_ok"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"p:{post_id}:reject"),
        ],
    ])


def manual_review_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔁 Повторить", callback_data=f"p:{post_id}:retry"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"p:{post_id}:reject"),
        ],
    ])


async def targets_keyboard(post_id: int) -> InlineKeyboardMarkup:
    """Выбор целевого канала при одобрении непривязанного поста."""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(TargetChannel).where(TargetChannel.enabled.is_(True)).order_by(TargetChannel.id)
            )
        ).scalars().all()
    if not rows:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Целевые каналы не заданы — добавьте в sources.yaml", callback_data="noop")]
        ])
    buttons = [
        [InlineKeyboardButton(text=f"@{t.username} — {t.title}", callback_data=f"p:{post_id}:to:{t.id}")]
        for t in rows
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def publish_mode_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚀 Сейчас", callback_data=f"p:{post_id}:mode:now"),
            InlineKeyboardButton(text="📥 В очередь", callback_data=f"p:{post_id}:mode:queue"),
        ],
        [
            InlineKeyboardButton(text="🕒 Отложить…", callback_data=f"p:{post_id}:mode:schedule"),
        ],
    ])