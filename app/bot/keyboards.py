from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


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