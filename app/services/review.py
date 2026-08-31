"""Сервис ревью — единая точка действий владельца (ТЗ §8).

Бот (Этап 4), будущий Mini App и админка вызывают эти же функции,
поэтому смена интерфейса не тронет бизнес-логику.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.db.enums import DraftOrigin, EventActor, PostStatus
from app.db.models import Post, PostDraftVersion, PostEvent, TargetChannel
from app.db.session import session_scope
from app.services.queue import enqueue_post

log = logging.getLogger(__name__)


@dataclass
class ActionResult:
    ok: bool
    message: str
    needs_target: bool = False


def _event(session, post_id, actor, action, from_status, to_status, details=None):
    session.add(PostEvent(
        post_id=post_id, actor=actor, action=action,
        from_status=from_status, to_status=to_status, details=details,
    ))


async def approve(post_id: int, target_channel_id: int | None = None) -> ActionResult:
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return ActionResult(False, "пост не найден")
        if post.status is not PostStatus.AWAITING_REVIEW:
            return ActionResult(False, f"недоступно в статусе {post.status.value}")

        if post.target_channel_id is None and target_channel_id is None:
            return ActionResult(False, "выберите целевой канал", needs_target=True)
        if target_channel_id is not None:
            channel_check = await session.get(TargetChannel, target_channel_id)
            if channel_check is None:
                return ActionResult(False, "канал не найден")
            post.target_channel_id = target_channel_id

        channel = await session.get(TargetChannel, post.target_channel_id)
        channel_username = channel.username if channel is not None else "?"

        post.status = PostStatus.APPROVED
        post.approved_at = datetime.now(timezone.utc)
        _event(session, post_id, EventActor.OWNER, "approved",
               PostStatus.AWAITING_REVIEW.value, PostStatus.APPROVED.value,
               {"target_channel": channel_username})
        await session.commit()

    log.info("пост %s одобрен владельцем для @%s", post_id, channel_username)
    return ActionResult(True, f"одобрено для канала @{channel_username} — выберите время публикации")


async def reject(post_id: int, reason: str = "") -> ActionResult:
    allowed = (
        PostStatus.AWAITING_REVIEW, PostStatus.NEEDS_MEDIA_REVIEW,
        PostStatus.NEEDS_MANUAL_REVIEW, PostStatus.REVISION, PostStatus.MANUAL_EDITING,
    )
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return ActionResult(False, "пост не найден")
        if post.status not in allowed:
            return ActionResult(False, f"недоступно в статусе {post.status.value}")
        from_status = post.status.value
        post.status = PostStatus.REJECTED
        post.reject_reason = (reason or "").strip() or "отклонено владельцем"
        _event(session, post_id, EventActor.OWNER, "rejected",
               from_status, PostStatus.REJECTED.value, {"reason": post.reject_reason})
        await session.commit()
    log.info("пост %s отклонён владельцем", post_id)
    return ActionResult(True, f"пост #{post_id} отклонён")


async def media_approve(post_id: int) -> ActionResult:
    """Визуальное подтверждение: пост уходит в рерайт и вернётся новой карточкой."""
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return ActionResult(False, "пост не найден")
        if post.status is not PostStatus.NEEDS_MEDIA_REVIEW:
            return ActionResult(False, f"недоступно в статусе {post.status.value}")
        post.needs_media_review = False
        post.status = PostStatus.CANDIDATE
        _event(session, post_id, EventActor.OWNER, "media_approved",
               PostStatus.NEEDS_MEDIA_REVIEW.value, PostStatus.CANDIDATE.value)
        await session.commit()
    await enqueue_post(post_id)  # пайплайн заберёт сразу, не ожидая рескана
    log.info("пост %s подтверждён визуально -> рерайт", post_id)
    return ActionResult(True, "подтверждено — черновик готовится, придёт новой карточкой")


async def retry_manual(post_id: int) -> ActionResult:
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return ActionResult(False, "пост не найден")
        if post.status is not PostStatus.NEEDS_MANUAL_REVIEW:
            return ActionResult(False, f"недоступно в статусе {post.status.value}")
        post.status = PostStatus.PREFILTERED
        _event(session, post_id, EventActor.OWNER, "retried",
               PostStatus.NEEDS_MANUAL_REVIEW.value, PostStatus.PREFILTERED.value)
        await session.commit()
    await enqueue_post(post_id)
    return ActionResult(True, "возвращён в пайплайн")


async def start_ai_revision(post_id: int) -> ActionResult:
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return ActionResult(False, "пост не найден")
        if post.status is not PostStatus.AWAITING_REVIEW:
            return ActionResult(False, f"недоступно в статусе {post.status.value}")
        if not post.draft_text:
            return ActionResult(False, "у поста ещё нет черновика")
        post.status = PostStatus.REVISION
        _event(session, post_id, EventActor.OWNER, "revision_requested",
               PostStatus.AWAITING_REVIEW.value, PostStatus.REVISION.value)
        await session.commit()
    return ActionResult(True, "")


async def start_manual_edit(post_id: int) -> ActionResult:
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return ActionResult(False, "пост не найден")
        if post.status is not PostStatus.AWAITING_REVIEW:
            return ActionResult(False, f"недоступно в статусе {post.status.value}")
        post.status = PostStatus.MANUAL_EDITING
        _event(session, post_id, EventActor.OWNER, "edit_requested",
               PostStatus.AWAITING_REVIEW.value, PostStatus.MANUAL_EDITING.value)
        await session.commit()
    return ActionResult(True, "")


async def cancel_interactive(post_id: int) -> None:
    """Возврат в ожидание ревью при отмене правки/редактора."""
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return
        if post.status in (PostStatus.REVISION, PostStatus.MANUAL_EDITING):
            from_status = post.status.value
            post.status = PostStatus.AWAITING_REVIEW
            _event(session, post_id, EventActor.OWNER, "interactive_cancelled",
                   from_status, PostStatus.AWAITING_REVIEW.value)
            await session.commit()


async def apply_manual_edit(post_id: int, text: str) -> ActionResult:
    text = (text or "").strip()
    if not text:
        return ActionResult(False, "пустой текст — черновик не обновлён")
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return ActionResult(False, "пост не найден")
        if post.status not in (PostStatus.MANUAL_EDITING, PostStatus.AWAITING_REVIEW):
            return ActionResult(False, f"недоступно в статусе {post.status.value}")
        post.draft_text = text
        post.draft_version += 1
        post.status = PostStatus.AWAITING_REVIEW
        session.add(PostDraftVersion(
            post_id=post_id, version=post.draft_version, text=text, origin=DraftOrigin.MANUAL,
        ))
        _event(session, post_id, EventActor.OWNER, "manual_edited", None,
               PostStatus.AWAITING_REVIEW.value, {"draft_version": post.draft_version})
        # карточка обновлена на месте — повторная рассылка не нужна
        _event(session, post_id, EventActor.SYSTEM, "card_sent", None, None,
               {"draft_version": post.draft_version})
        await session.commit()
    return ActionResult(True, "черновик обновлён вручную")


async def apply_ai_revision(post_id: int, comment: str) -> ActionResult:
    from app.services.llm_pipeline import revise_draft  # локально против циклов

    ok, message = await revise_draft(post_id, (comment or "").strip())
    return ActionResult(ok, message)


async def mark_card_sent(post_id: int, draft_version: int) -> None:
    async with session_scope() as session:
        _event(session, post_id, EventActor.SYSTEM, "card_sent", None, None,
               {"draft_version": draft_version})
        await session.commit()