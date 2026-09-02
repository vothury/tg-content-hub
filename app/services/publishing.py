"""Публикация в целевые каналы (Этап 5).

Режимы: сейчас / очередь / расписание. Задачи живут в publish_jobs;
повторное создание блокируется уникальным idempotency_key ('post-{id}').
Лимиты канала: максимум в день, минимальный интервал, тихие часы.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo
from sqlalchemy import func, select, update

from app.config import settings
from app.db.enums import EventActor, MediaType, PostStatus, PublishJobState, PublishMode
from app.db.models import MediaItem, Post, PostEvent, PublishJob, TargetChannel
from app.db.session import session_scope
from app.services.times import owner_now

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024


def _media_root() -> Path:
    root = Path(settings.media_dir)
    if not root.is_absolute():
        root = Path("/app") / settings.media_dir
    return root


def _in_quiet_hours(channel: TargetChannel, now_local: datetime) -> bool:
    qh = channel.quiet_hours or {}
    start_s, end_s = qh.get("start"), qh.get("end")
    if not start_s or not end_s:
        return False
    try:
        start = datetime.strptime(start_s, "%H:%M").time()
        end = datetime.strptime(end_s, "%H:%M").time()
    except ValueError:
        return False
    cur = now_local.time()
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end  # ночной диапазон через полночь


# Отсрочки: не перепроверяем задачу до указанного времени
# и не повторяем одно и то же уведомление.
_deferred_until: dict[int, datetime] = {}
_last_defer_reason: dict[int, str] = {}


def _owner_chat_id() -> int | None:
    return settings.allowed_owner_ids[0] if settings.allowed_owner_ids else None


async def _notify_owner(bot, text: str) -> None:
    chat_id = _owner_chat_id()
    if bot is None or chat_id is None:
        return
    try:
        await bot.send_message(chat_id, text)
    except Exception:  # noqa: BLE001 — уведомление не должно ломать публикацию
        log.warning("не удалось отправить уведомление владельцу: %s", text[:120])


def _until_next_day() -> timedelta:
    now_local = owner_now()
    next_midnight = (now_local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return next_midnight - now_local


async def create_publish_job(post_id: int, mode: PublishMode, scheduled_at: datetime | None = None) -> tuple[bool, str]:
    """Создаёт задачу публикации идемпотентно; для упавших — перезапуск."""
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return False, "пост не найден"
        if post.status not in (PostStatus.APPROVED, PostStatus.SCHEDULED, PostStatus.FAILED):
            return False, f"пост не готов к публикации (статус {post.status.value})"
        if post.target_channel_id is None:
            return False, "у поста не выбран целевой канал"

        existing = (
            await session.execute(select(PublishJob).where(PublishJob.post_id == post_id))
        ).scalars().all()
        active = [j for j in existing if j.state in (
            PublishJobState.QUEUED, PublishJobState.SCHEDULED, PublishJobState.IN_PROGRESS)]
        done = [j for j in existing if j.state is PublishJobState.DONE]
        failed = [j for j in existing if j.state is PublishJobState.FAILED]

        if done:
            return False, "пост уже опубликован"
        if active:
            return True, "задача публикации уже создана и ждёт выполнения"

        new_state = PublishJobState.SCHEDULED if mode is PublishMode.SCHEDULE else PublishJobState.QUEUED
        if failed:
            job = failed[-1]
            job.state = new_state
            job.mode = mode
            job.scheduled_at = scheduled_at
            job.attempts = 0
            job.last_error = None
        else:
            job = PublishJob(
                post_id=post_id,
                target_channel_id=post.target_channel_id,
                idempotency_key=f"post-{post_id}",
                mode=mode,
                scheduled_at=scheduled_at,
                state=new_state,
                max_attempts=3,
            )
            session.add(job)

        if mode is PublishMode.SCHEDULE:
            post.status = PostStatus.SCHEDULED
        elif post.status is PostStatus.FAILED:
            post.status = PostStatus.APPROVED
        session.add(PostEvent(
            post_id=post_id, actor=EventActor.OWNER, action="publish_mode_selected",
            details={"mode": mode.value,
                     "scheduled_at": scheduled_at.isoformat() if scheduled_at else None},
        ))
        await session.commit()

    if mode is PublishMode.SCHEDULE:
        return True, "запланирован — статус сообщу"
    if mode is PublishMode.NOW:
        return True, "публикую — статус сообщу"
    return True, "в очереди — статус сообщу"


async def _resolve_channel_id(bot: Bot, channel: TargetChannel) -> int | None:
    if channel.telegram_id:
        return channel.telegram_id
    if not channel.username:
        return None
    try:
        chat = await bot.get_chat(f"@{channel.username}")
    except Exception as exc:  # noqa: BLE001
        log.error("канал @%s: чат недоступен (%s). Бот должен быть админом канала.",
                  channel.username, exc)
        return None
    async with session_scope() as session:
        ch = await session.get(TargetChannel, channel.id)
        if ch is not None:
            ch.telegram_id = chat.id
            ch.last_admin_check_at = datetime.now(timezone.utc)
            await session.commit()
    return chat.id


async def _channel_allows(channel_id: int) -> tuple[bool, str, timedelta | None]:
    """Разрешена ли публикация в канал прямо сейчас.
    Возвращает (разрешено, причина, через сколько перепроверить)."""
    async with session_scope() as session:
        channel = await session.get(TargetChannel, channel_id)
        if channel is None:
            return False, "канал не найден", timedelta(minutes=15)
        now = owner_now()
        if _in_quiet_hours(channel, now):
            return False, "тихие часы", timedelta(minutes=15)
        day_start_utc = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        published_today = await session.scalar(
            select(func.count()).select_from(PublishJob).where(
                PublishJob.target_channel_id == channel_id,
                PublishJob.state == PublishJobState.DONE,
                PublishJob.published_at >= day_start_utc,
            )
        )
        if published_today is not None and published_today >= channel.daily_limit:
            return False, f"лимит {channel.daily_limit} публикаций в день исчерпан", _until_next_day()
        last = await session.scalar(
            select(func.max(PublishJob.published_at)).where(
                PublishJob.target_channel_id == channel_id,
                PublishJob.state == PublishJobState.DONE,
            )
        )
        if last is not None and (datetime.now(timezone.utc) - last) < timedelta(minutes=channel.min_interval_min):
            remaining = timedelta(minutes=channel.min_interval_min) - (datetime.now(timezone.utc) - last)
            return False, f"минимальный интервал {channel.min_interval_min} мин ещё не прошёл", remaining
    return True, "", None


async def _next_candidates() -> list[tuple[int, int, int]]:
    now_utc = datetime.now(timezone.utc)
    async with session_scope() as session:
        rows = (await session.execute(
            select(PublishJob.id, PublishJob.target_channel_id, PublishJob.post_id)
            .where(
                (PublishJob.state == PublishJobState.QUEUED)
                | ((PublishJob.state == PublishJobState.SCHEDULED) & (PublishJob.scheduled_at <= now_utc))
            )
            .order_by(PublishJob.mode != PublishMode.NOW, PublishJob.scheduled_at.asc(), PublishJob.id)
            .limit(10)
        )).all()
        return [(r.id, r.target_channel_id, r.post_id) for r in rows]


async def _claim_job(job_id: int) -> bool:
    async with session_scope() as session:
        result = await session.execute(
            update(PublishJob)
            .where(PublishJob.id == job_id,
                   PublishJob.state.in_([PublishJobState.QUEUED, PublishJobState.SCHEDULED]))
            .values(state=PublishJobState.IN_PROGRESS)
        )
        await session.commit()
        return result.rowcount == 1


async def _send_to_channel(bot: Bot, chat_id: int, post: Post) -> int:
    """Отправка поста (медиа + текст) без parse-режима. Возвращает id сообщения."""
    text = post.draft_text or post.original_text or ""
    root = _media_root()
    media = (
        await _select_media(post.id)
    )
    files: list = []
    first = True
    for m in media:
        if not m["downloaded"] or not m["local_path"]:
            continue
        path = root / m["local_path"]
        if not path.exists():
            continue
        caption = text[:CAPTION_LIMIT] if first else None
        if m["media_type"] is MediaType.VIDEO:
            files.append(InputMediaVideo(media=FSInputFile(path), caption=caption))
        else:
            files.append(InputMediaPhoto(media=FSInputFile(path), caption=caption))
        first = False

    if files:
        sent = await bot.send_media_group(chat_id, media=files)
        published_id = sent[0].message_id
        if len(text) > CAPTION_LIMIT:
            await bot.send_message(chat_id, text[CAPTION_LIMIT:])
        return published_id
    message = await bot.send_message(chat_id, text)
    return message.message_id


async def _select_media(post_id: int) -> list[dict]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(MediaItem).where(MediaItem.post_id == post_id).order_by(MediaItem.position)
            )
        ).scalars().all()
        return [
            {"media_type": r.media_type, "local_path": r.local_path, "downloaded": r.downloaded}
            for r in rows
        ]


async def _finish_failed(bot, job_id: int, error: str, attempts: int, final: bool) -> None:
    async with session_scope() as session:
        job = await session.get(PublishJob, job_id)
        if job is None:
            return
        if final or job.attempts >= job.max_attempts:
            job.state = PublishJobState.FAILED
            is_final = True
        else:
            job.state = PublishJobState.QUEUED
            is_final = False
        post_id = job.post_id
        await session.commit()

    if is_final:
        async with session_scope() as session:
            post = await session.get(Post, post_id)
            if post is not None:
                post.status = PostStatus.FAILED
                session.add(PostEvent(
                    post_id=post_id, actor=EventActor.SYSTEM, action="publish_failed",
                    to_status=PostStatus.FAILED.value, details={"error": error, "attempts": attempts},
                ))
                await session.commit()
        log.error("задача %s: публикация не удалась после %d попыток: %s", job_id, attempts, error)
        await _notify_owner(bot, f"⚠️ Пост #{post_id}: не удалось опубликовать после {attempts} попыток — {error[:150]}")
    else:
        log.warning("задача %s: ошибка публикации (попытка %d): %s", job_id, attempts, error)


async def _publish(bot: Bot, job_id: int) -> None:
    async with session_scope() as session:
        job = await session.get(PublishJob, job_id)
        if job is None or job.state is not PublishJobState.IN_PROGRESS:
            return
        post_id = job.post_id
        channel_id = job.target_channel_id
        job.attempts += 1
        attempts = job.attempts
        await session.commit()

    async with session_scope() as session:
        post = await session.get(Post, post_id)
        channel = await session.get(TargetChannel, channel_id)
    if post is None or channel is None:
        await _finish_failed(bot, job_id, "пост или канал не найдены", attempts, final=True)
        return

    chat_id = await _resolve_channel_id(bot, channel)
    if chat_id is None:
        await _finish_failed(bot, job_id, "канал недоступен: бот должен быть админом", attempts, final=False)
        return

    try:
        published_id = await _send_to_channel(bot, chat_id, post)
    except Exception as exc:  # noqa: BLE001
        await _finish_failed(bot, job_id, f"{exc.__class__.__name__}: {exc}", attempts, final=False)
        return

    async with session_scope() as session:
        job = await session.get(PublishJob, job_id)
        post = await session.get(Post, post_id)
        if job is None or post is None:
            return
        job.state = PublishJobState.DONE
        job.defer_reason = None
        job.published_message_id = published_id
        job.published_at = datetime.now(timezone.utc)
        post.status = PostStatus.PUBLISHED
        session.add(PostEvent(
            post_id=post_id, actor=EventActor.SYSTEM, action="published",
            from_status=None, to_status=PostStatus.PUBLISHED.value,
            details={"channel": channel.username, "message_id": published_id, "attempts": attempts},
        ))
        await session.commit()
    log.info("пост %s опубликован в @%s (сообщение %s)", post_id, channel.username, published_id)
    await _notify_owner(bot, f"✅ Пост #{post_id} опубликован в @{channel.username}")


async def process_ready_jobs(bot) -> None:
    now = datetime.now(timezone.utc)
    for job_id, channel_id, post_id in await _next_candidates():
        skip_until = _deferred_until.get(job_id)
        if skip_until is not None and now < skip_until:
            continue  # задача в отсрочке — молча ждём

        allowed, reason, retry_after = await _channel_allows(channel_id)
        if not allowed:
            _deferred_until[job_id] = now + (retry_after or timedelta(minutes=15))
            if _last_defer_reason.get(job_id) != reason:
                log.info("задача %s (пост %s) отложена: %s", job_id, post_id, reason)
                await _notify_owner(bot, f"⏳ Пост #{post_id}: публикация отложена — {reason}")
                _last_defer_reason[job_id] = reason
                async with session_scope() as session:
                    j = await session.get(PublishJob, job_id)
                    if j is not None:
                        j.defer_reason = reason
                        await session.commit()
            continue

        _deferred_until.pop(job_id, None)
        _last_defer_reason.pop(job_id, None)
        if not await _claim_job(job_id):
            continue
        await _publish(bot, job_id)