"""Сервис ревью — единая точка действий владельца (ТЗ §8).

Бот (Этап 4), будущий Mini App и админка вызывают эти же функции,
поэтому смена интерфейса не тронет бизнес-логику.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, select, update

from app.db.enums import DraftOrigin, EventActor, PostStatus, PublishJobState
from app.db.models import (
    MediaItem, Post, PostDraftVersion, PostEvent, PublishJob, TargetChannel,
)
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
        if post.status not in (PostStatus.AWAITING_REVIEW, PostStatus.DOUBLE_CHECK_REVIEW):
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
        post.autopilot = False   # одобрено владельцем, а не автопилотом
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
        PostStatus.DOUBLE_CHECK_REVIEW,
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


async def revive_from_dedup(post_id: int) -> ActionResult:
    """Ложный дубль: вернуть в работу и перескачать медиа заново."""
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return ActionResult(False, "пост не найден")
        if post.status is not PostStatus.DEDUPLICATED:
            return ActionResult(False, f"недоступно в статусе {post.status.value}")
        post.status = PostStatus.CANDIDATE
        post.dedup_info = None
        post.needs_media_refresh = True
        for m in (await session.execute(
                select(MediaItem).where(MediaItem.post_id == post_id))).scalars().all():
            await session.delete(m)
        _event(session, post_id, EventActor.OWNER, "dedup_revived",
               PostStatus.DEDUPLICATED.value, PostStatus.CANDIDATE.value)
        await session.commit()
    return ActionResult(True, "возвращён в работу; медиа будут скачаны заново")


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


async def hard_delete(post_id: int) -> ActionResult:
    """Полное удаление поста: медиафайлы, все связанные строки и сама запись.

    Восстановление невозможно. Заголовки виртуальной редакции сохраняются
    (у них обнуляется post_id), чтобы не ломать поток редакции.
    """
    from app.db.models import Headline, LLMCall, MediaItem, PostDraftVersion, PublishJob
    from app.services.publishing import _media_root
    from app.services.security import is_hard_delete_armed

    if not await is_hard_delete_armed():
        log.warning("запрошено полное удаление поста %s при выключенном предохранителе", post_id)
        return ActionResult(False, "полное удаление отключено (предохранитель)")

    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return ActionResult(False, "пост не найден")
        if post.status is PostStatus.PUBLISHING:
            return ActionResult(False, "пост публикуется прямо сейчас — дождитесь завершения")
        status_before = post.status.value
        files = (await session.execute(
            select(MediaItem.local_path, MediaItem.preview_path)
            .where(MediaItem.post_id == post_id))).all()
        for model in (LLMCall, PostEvent, PostDraftVersion, MediaItem, PublishJob):
            await session.execute(delete(model).where(model.post_id == post_id))
        await session.execute(update(Headline).where(Headline.post_id == post_id)
                              .values(post_id=None))
        await session.delete(post)
        await session.commit()

    root = _media_root()
    removed = 0
    for local_path, preview_path in files:
        for rel in (local_path, preview_path):
            if not rel:
                continue
            p = root / rel
            try:
                if p.exists():
                    p.unlink()
                    removed += 1
                for d in (p.parent, p.parent.parent):
                    if d.exists() and d != root and not any(d.iterdir()):
                        d.rmdir()
            except Exception:  # noqa: BLE001 — удаление файла не критично
                log.warning("не удалось удалить медиафайл %s", p)
    log.info("пост %s удалён полностью (был %s; медиафайлов удалено: %d)",
             post_id, status_before, removed)
    return ActionResult(True, f"пост #{post_id} удалён из базы полностью")


async def restart_pipeline(post_id: int) -> ActionResult:
    """Полный перезапуск: пост сбрасывается в NEW, как если бы его только что прочитали.

    Очищаются результаты анализа (канон, оценка, вердикт, дедуп, recap, черновик),
    отменяются незавершённые задачи публикации; если медиа были удалены после
    публикации/дедупа — запрашивается их рескачка (reader.process_media_refresh).
    """
    blocked = (PostStatus.PUBLISHED, PostStatus.PUBLISHING,
               PostStatus.SCHEDULED, PostStatus.APPROVED)
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return ActionResult(False, "пост не найден")
        if post.status in blocked:
            return ActionResult(False,
                                f"недоступно в статусе {post.status.value} — "
                                "сначала отмените публикацию")
        from_status = post.status.value
        cleared = []
        for field in ("canonical_text", "score", "verdict_reason", "dedup_info",
                      "recap_ids", "draft_text", "reject_reason", "double_check_note"):
            if hasattr(post, field) and getattr(post, field) is not None:
                cleared.append(field)
                setattr(post, field, None)
        post.status = PostStatus.NEW
        post.approved_at = None
        post.needs_media_review = False
        post.autopilot = False

        media = (await session.execute(
            select(MediaItem).where(MediaItem.post_id == post_id))).scalars().all()
        if media and any(m.local_path is None for m in media):
            for m in media:
                await session.delete(m)
            post.needs_media_refresh = True
            cleared.append("media_items")
        else:
            post.needs_media_refresh = False

        cancelled = 0
        jobs = (await session.execute(
            select(PublishJob).where(
                PublishJob.post_id == post_id,
                PublishJob.state.in_([PublishJobState.QUEUED, PublishJobState.SCHEDULED,
                                      PublishJobState.IN_PROGRESS])))).scalars().all()
        for job in jobs:
            job.state = PublishJobState.FAILED
            job.last_error = "отменено: повтор обработки"
            cancelled += 1

        _event(session, post_id, EventActor.OWNER, "restart_pipeline",
               from_status, PostStatus.NEW.value,
               {"cleared": cleared, "cancelled_jobs": cancelled})
        await session.commit()

    await enqueue_post(post_id)
    log.info("пост %s: перезапуск конвейера (был %s; сброшено: %s)",
             post_id, from_status, ", ".join(cleared) or "—")
    return ActionResult(True, f"перезапущен с нуля (был {from_status}) — статус NEW")


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
        if post.status not in (PostStatus.AWAITING_REVIEW, PostStatus.DOUBLE_CHECK_REVIEW):
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
        if post.status not in (PostStatus.AWAITING_REVIEW, PostStatus.DOUBLE_CHECK_REVIEW):
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
        if post.status not in (PostStatus.MANUAL_EDITING, PostStatus.AWAITING_REVIEW,
                               PostStatus.DOUBLE_CHECK_REVIEW):
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


async def mark_card_sent(post_id: int, draft_version: int, message_id: int | None = None) -> None:
    async with session_scope() as session:
        _event(session, post_id, EventActor.SYSTEM, "card_sent", None, None,
               {"draft_version": draft_version, "message_id": message_id})
        await session.commit()