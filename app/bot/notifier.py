"""Рассылка карточек: посты, ждущие ревью, без отправленной карточки
на текущую версию черновика. Плюс Web Push в PWA для новых AWAITING_REVIEW."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import Integer, select

from app.bot.cards import send_card
from app.config import settings
from app.db.enums import PostStatus
from app.db.models import Post, PostEvent
from app.db.session import session_scope
from app.services import webpush

log = logging.getLogger(__name__)

CARD_STATUSES = (PostStatus.AWAITING_REVIEW, PostStatus.NEEDS_MEDIA_REVIEW, PostStatus.NEEDS_MANUAL_REVIEW)

_forbidden_warned = False


async def pending_card_post_ids(limit: int = 5) -> list[int]:
    async with session_scope() as session:
        already_sent = (
            select(PostEvent.id)
            .where(PostEvent.post_id == Post.id)
            .where(PostEvent.action == "card_sent")
            .where(PostEvent.details["draft_version"].astext.cast(Integer) == Post.draft_version)
            .exists()
        )
        rows = await session.execute(
            select(Post.id)
            .where(Post.status.in_(CARD_STATUSES))
            .where(~already_sent)
            .order_by(Post.id)
            .limit(limit)
        )
        return list(rows.scalars().all())


async def _post_status(post_id: int):
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        return post.status if post is not None else None


async def card_notifier(bot: Bot) -> None:
    global _forbidden_warned
    if not settings.allowed_owner_ids:
        log.error("ALLOWED_OWNER_IDS пуст — карточки отправлять некому, рассылка не запущена")
        return
    owner_chat = settings.allowed_owner_ids[0]
    while True:
        try:
            post_ids = await pending_card_post_ids()
            for post_id in post_ids:
                try:
                    await send_card(bot, owner_chat, post_id)
                    log.info("карточка поста %s отправлена", post_id)
                    if await _post_status(post_id) is PostStatus.AWAITING_REVIEW:
                        await webpush.notify_all(
                            "Новый пост на ревью", f"#{post_id} ожидает вашего решения"
                        )
                except TelegramForbiddenError:
                    if not _forbidden_warned:
                        _forbidden_warned = True
                        log.warning(
                            "Telegram не позволяет боту писать владельцу (чат %s). "
                            "Откройте бота и отправьте ему /start — карточки придут автоматически.",
                            owner_chat,
                        )
                    break  # остальные посты этого цикла тоже не доставить
                except Exception:  # noqa: BLE001
                    log.exception("не удалось отправить карточку поста %s", post_id)
        except Exception:  # noqa: BLE001
            log.exception("сбой цикла рассылки карточек")
        await asyncio.sleep(settings.review_poll_interval_sec)