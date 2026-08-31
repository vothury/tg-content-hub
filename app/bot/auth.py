"""Доступ: действия ревью только для владельцев из ALLOWED_OWNER_IDS (ТЗ §13)."""
from __future__ import annotations

import logging

from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.config import settings

log = logging.getLogger(__name__)


class OwnerOnlyMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = getattr(event, "from_user", None)
        if user is None or user.id not in settings.allowed_owner_ids:
            log.warning("запрос от постороннего пользователя: %s", getattr(user, "id", "?"))
            if isinstance(event, CallbackQuery):
                await event.answer("Доступ запрещён", show_alert=True)
            # сообщения посторонних игнорируем молча
            return None
        return await handler(event, data)