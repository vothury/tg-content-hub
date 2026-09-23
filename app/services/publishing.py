"""Публикация в целевые каналы (Этап 5).

Режимы: сейчас / очередь / расписание. Задачи живут в publish_jobs;
повторное создание блокируется уникальным idempotency_key ('post-{id}').
Лимиты канала: максимум в день, минимальный интервал, тихие часы.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo, MessageEntity
from sqlalchemy import func, select, update

from app.config import settings
from app.db.enums import EventActor, MediaType, PostStatus, PublishJobState, PublishMode
from app.db.models import MediaItem, Post, PostEvent, PublishJob, Source, TargetChannel
from app.db.session import session_scope
from app.services.times import owner_now, owner_tz
from app.services.text import html_to_text
from app.services.settings import Keys, get_setting

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


class OversizedMedia(Exception):
    """Медиа больше лимита — публикация без сжатия/пропуска невозможна (не повторяем)."""


def _file_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _safe_unlink(p: Path) -> None:
    try:
        if p.exists():
            p.unlink()
    except OSError:
        log.warning("не удалось удалить временный файл %s", p)


async def _job_note(job_id, text) -> None:
    """Заметка о текущем действии: видна в карточке поста и в /queue,
    а также меняет сигнатуру опроса — страница сама обновится."""
    if not job_id:
        return
    async with session_scope() as session:
        job = await session.get(PublishJob, job_id)
        if job is not None:
            job.defer_reason = text
            await session.commit()


async def _post_event(post_id: int, action: str, details: dict | None = None) -> None:
    async with session_scope() as session:
        session.add(PostEvent(post_id=post_id, actor=EventActor.SYSTEM,
                              action=action, details=details or {}))
        await session.commit()


async def _media_settings():
    async with session_scope() as session:
        max_mb = int(await get_setting(session, Keys.PUBLISH_MAX_MEDIA_MB))
        compress = int(await get_setting(session, Keys.PUBLISH_MEDIA_COMPRESS))
        target_mb = int(await get_setting(session, Keys.PUBLISH_COMPRESS_TARGET_MB))
        max_side = int(await get_setting(session, Keys.PUBLISH_COMPRESS_MAX_SIDE))
        skip = int(await get_setting(session, Keys.PUBLISH_SKIP_OVERSIZED))
    return max_mb, compress, target_mb, max_side, skip


async def _compress_video(src: Path, target_mb: int, max_side: int):
    """Пересборка видео ffmpeg под целевой размер. None, если сжать не удалось."""
    import asyncio
    import subprocess

    loop = asyncio.get_running_loop()

    def _probe() -> float:
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(src)],
                capture_output=True, text=True, timeout=60)
            return float((out.stdout or "0").strip() or 0)
        except Exception:  # noqa: BLE001
            return 0.0

    duration = await loop.run_in_executor(None, _probe)
    if duration <= 0:
        log.warning("сжатие видео: не удалось определить длительность %s", src.name)
        return None
    target_bytes = max(1024 * 1024, int(target_mb * 1024 * 1024 * 0.95))
    audio_kbps = 96
    video_kbps = int(target_bytes * 8 / duration / 1000) - audio_kbps
    if video_kbps < 120:
        log.warning("сжатие видео %s: целевой битрейт слишком мал (%d kbps) — не сжимаем",
                    src.name, video_kbps)
        return None
    log.info("ffmpeg: сжимаю %s (%.1f МБ, длительность %.0f с) -> цель %d МБ, "
             "видео %d kbps, сторона<=%d",
             src.name, _file_size(src) / 1048576, duration, target_mb, video_kbps, max_side)
    started = time.monotonic()
    dst = src.with_name(f"{src.stem}_{target_mb}mb.mp4")

    def _run() -> None:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
             "-vf", f"scale='min({max_side},iw)':-2",
             "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{video_kbps}k",
             "-maxrate", f"{int(video_kbps * 1.3)}k", "-bufsize", f"{video_kbps * 2}k",
             "-c:a", "aac", "-b:a", f"{audio_kbps}k", "-movflags", "+faststart",
             str(dst)],
            check=True, timeout=1800, capture_output=True)

    try:
        await loop.run_in_executor(None, _run)
    except Exception as exc:  # noqa: BLE001
        log.warning("ffmpeg не смог сжать %s: %s: %s", src.name, exc.__class__.__name__, exc)
        _safe_unlink(dst)
        return None
    if not dst.exists() or _file_size(dst) == 0:
        log.warning("ffmpeg: %s — результат пустой", src.name)
        return None
    log.info("ffmpeg: готово %s за %.1f с: %.1f -> %.1f МБ",
             dst.name, time.monotonic() - started,
             _file_size(src) / 1048576, _file_size(dst) / 1048576)
    return dst


def _media_root() -> Path:
    root = Path(settings.media_dir)
    if not root.is_absolute():
        root = Path("/app") / settings.media_dir
    return root


async def purge_post_media(post_id: int) -> None:
    """После публикации файлы медиа не нужны: удаляем с диска.
    Строки MediaItem (тип, phash) остаются для дедупликации и истории."""
    root = _media_root()
    async with session_scope() as session:
        rows = (await session.execute(
            select(MediaItem).where(MediaItem.post_id == post_id)
        )).scalars().all()
        paths = [r.local_path for r in rows if r.local_path]
        for r in rows:
            r.local_path = None
            r.downloaded = False
        await session.commit()
    for rel in paths:
        p = root / rel
        try:
            if p.exists():
                p.unlink()
            # подчищаем пустые папки поста/источника
            for d in (p.parent, p.parent.parent):
                if d.exists() and not any(d.iterdir()):
                    d.rmdir()
        except Exception:  # noqa: BLE001 — очистка не должна ломать публикацию
            log.warning("не удалось удалить медиафайл %s", p)


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


async def _credit_line(post_id: int):
    """Программная подпись источника для технических каналов (не LLM)."""
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        src = await session.get(Source, post.source_id) if post is not None and post.source_id else None
        ch = await session.get(TargetChannel, post.target_channel_id) if post is not None and post.target_channel_id else None
    if src is None or ch is None or not ch.no_review or ch.aggregate_mode != "credit":
        return None
    name = src.title or (f"@{src.username}" if src.username else "источник")
    url = (f"https://t.me/{src.username}/{post.source_message_id}"
           if src.username and post.source_message_id else None)
    return name, url


async def _recap_rows(post_id: int) -> list:
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        ids = list(post.recap_ids or []) if post is not None else []
        if not ids:
            return []
        rows = (await session.execute(
            select(Post.id, Post.post_url, Post.source_published_at, Source.title, Source.username)
            .select_from(Post)
            .join(Source, Source.id == Post.source_id)
            .where(Post.id.in_(ids)))).all()
    return [tuple(r) for r in rows]


def _md_to_entities(text: str):
    """Markdown [якорь](url) -> чистый текст + text_link-сущности Telegram."""
    entities = []
    out = []
    pos = 0
    for m in _MD_LINK_RE.finditer(text):
        out.append(text[pos:m.start()])
        anchor = m.group(1)
        entities.append(MessageEntity(type="text_link", offset=len("".join(out)),
                                      length=len(anchor), url=m.group(2)))
        out.append(anchor)
        pos = m.end()
    out.append(text[pos:])
    return "".join(out), entities


_SIG_LINE_RE = re.compile(r"подписат|подписывай|subscribe|наш канал|подробнее", re.I)


def _info_links(original: str) -> list:
    """Только информационные ссылки: внутри предложения; подписи/CTA игнорируем."""
    out = []
    for line in (original or "").splitlines():
        stripped = line.strip()
        if not stripped or _SIG_LINE_RE.search(stripped):
            continue
        for m in _MD_LINK_RE.finditer(line):
            rest = _MD_LINK_RE.sub("", line).strip()
            if len(rest) < 25:  # строка состоит почти только из ссылки — это подпись
                continue
            out.append(m.group(2))
    return out


async def _restore_lost_links(post, text: str, entities: list):
    """Страховка: если модель съела ВСЕ информационные ссылки — вернуть их строкой.
    Подписи/CTA и строки-ссылки не восстанавливаем никогда."""
    async with session_scope() as session:
        enabled = int(await get_setting(session, Keys.PUBLISH_RESTORE_LINKS))
    if not enabled:
        return text, entities
    urls = _info_links(post.original_text or "")
    if not urls:
        return text, entities
    if entities or ("http" in text) or ("t.me/" in text):
        return text, entities
    return text + "\n\nПодробнее: " + ", ".join(dict.fromkeys(urls)), entities




def _build_recap(text: str, rows: list) -> tuple[str, list]:
    rows = sorted(rows, key=lambda r: r[2] or datetime.min.replace(tzinfo=timezone.utc))
    base = text.rstrip()
    out = base + "\n\nРанее об этом уже писали:"
    entities = []
    for rid, url, dt, title, uname in rows:
        name = (title or uname or f"источник {rid}")
        when = dt.astimezone(owner_tz()).strftime("%d.%m, %H:%M") if dt else "—"
        line = f"\n• {name} — {when}"
        offset = len(out) + 3  # "\n• " = 3 символа
        out += line
        if url:
            entities.append(MessageEntity(type="text_link", offset=offset, length=len(name), url=url))
    return out, entities


def _shift_entities(entities: list, cut: int) -> list:
    out = []
    for e in entities:
        if e.offset >= cut:
            e.offset -= cut
            out.append(e)
    return out


def _split_caption(text: str) -> tuple[str, str]:
    """Режем подпись по границе абзаца/предложения/слова, а не посреди слова."""
    if len(text) <= CAPTION_LIMIT:
        return text, ""
    cut = text[:CAPTION_LIMIT]
    for sep in ("\n\n", "\n", ". ", " "):
        idx = cut.rfind(sep)
        if idx > int(CAPTION_LIMIT * 0.6):
            return text[:idx].rstrip(), text[idx:].lstrip()
    return cut, text[CAPTION_LIMIT:]


async def _send_to_channel(bot: Bot, chat_id: int, post: Post,
                           job_id: int | None = None) -> tuple[int, str, list]:
    """Отправка поста (медиа + текст + блок «ранее писали») без parse-режима.

    Возвращает (message_id, итоговый текст, ссылки) — они сохраняются в пост,
    чтобы в карточке было видно, что именно опубликовано в канале.
    """
    raw = html_to_text(post.draft_text or post.original_text or "") or ""
    text, entities = _md_to_entities(raw)
    text, entities = await _restore_lost_links(post, text, entities)
    # Ссылки публикации: из markdown исходника + добавленные строкой «Подробнее:»
    links = [{"anchor": m.group(1), "url": m.group(2)} for m in _MD_LINK_RE.finditer(raw)]
    for m in re.finditer(r"https?://[^\s)]+", text):
        u = m.group(0).rstrip(".,")
        if not any(l["url"] == u for l in links):
            links.append({"anchor": u, "url": u})
    rows = await _recap_rows(post.id)
    if rows:
        text, entities = _build_recap(text, rows)
    credit = await _credit_line(post.id)
    if credit:
        name, url = credit
        prefix = "\n\nИсточник: "
        offset = len(text) + len(prefix)
        text = text + prefix + name
        if url:
            entities.append(MessageEntity(type="text_link", offset=offset,
                                          length=len(name), url=url))
            links.append({"anchor": name, "url": url})
    for rid, rurl, _rdate, rtitle, runame in rows:
        if rurl:
            links.append({"anchor": (rtitle or runame or f"источник {rid}"), "url": rurl})
    root = _media_root()
    media = await _select_media(post.id)
    max_mb, compress_on, target_mb, max_side, skip_on = await _media_settings()
    max_bytes = max_mb * 1024 * 1024
    files: list = []
    first = True
    total_bytes = 0
    tmp_files: list = []
    skipped: list = []
    for m in media:
        if not m["downloaded"] or not m["local_path"]:
            continue
        path = root / m["local_path"]
        if not path.exists():
            continue
        send_path = path
        size = _file_size(path)
        if max_bytes and size > max_bytes:
            compressed = None
            if compress_on and m["media_type"] is MediaType.VIDEO:
                goal_mb = min(target_mb or max_mb, max_mb)
                await _job_note(job_id, f"сжатие видео ffmpeg: {size / 1048576:.1f} МБ → цель {goal_mb} МБ")
                await _post_event(post.id, "media_compress_started",
                                  {"file": path.name, "mb": round(size / 1048576, 1),
                                   "target_mb": goal_mb})
                started = time.monotonic()
                compressed = await _compress_video(path, goal_mb, max_side)
                elapsed = round(time.monotonic() - started, 1)
                if compressed is not None:
                    await _post_event(post.id, "media_compress_done",
                                      {"file": compressed.name, "seconds": elapsed,
                                       "mb": round(_file_size(compressed) / 1048576, 1)})
                else:
                    await _post_event(post.id, "media_compress_failed",
                                      {"file": path.name, "seconds": elapsed})
                await _job_note(job_id, None)
            if compressed is not None and _file_size(compressed) <= max_bytes:
                send_path = compressed
                tmp_files.append(compressed)
                log.info("пост %s: видео сжато %.1f -> %.1f МБ",
                         post.id, size / 1048576, _file_size(compressed) / 1048576)
            else:
                if compressed is not None:
                    _safe_unlink(compressed)
                if skip_on:
                    skipped.append(f"{path.name} ({size / 1048576:.1f} МБ)")
                    continue
                raise OversizedMedia(
                    f"{path.name}: {size / 1048576:.1f} МБ больше лимита {max_mb} МБ"
                    + ("" if compress_on else " (сжатие видео выключено)")
                    + ("" if skip_on else " (пропуск oversized выключен)"))
        size = _file_size(send_path)
        if max_bytes and total_bytes + size > max_bytes and files:
            skipped.append(f"{send_path.name} (превышен суммарный лимит альбома)")
            continue
        total_bytes += size
        if first and len(text) <= CAPTION_LIMIT:
            caption, cap_entities = text, (entities or None)
        elif first:
            caption, _rest = _split_caption(text)
            cap_entities = [e for e in entities if e.offset + e.length <= len(caption)] or None
        else:
            caption, cap_entities = None, None
        if m["media_type"] is MediaType.VIDEO:
            files.append(InputMediaVideo(media=FSInputFile(send_path), caption=caption,
                                         caption_entities=cap_entities))
        else:
            files.append(InputMediaPhoto(media=FSInputFile(send_path), caption=caption,
                                         caption_entities=cap_entities))
        first = False

    if skipped:
        log.warning("пост %s: медиа пропущены при публикации: %s", post.id, ", ".join(skipped))
    try:
        if files:
            sent = await bot.send_media_group(chat_id, media=files)
            published_id = sent[0].message_id
            caption_part, rest_part = _split_caption(text)
            if rest_part:
                await bot.send_message(chat_id, rest_part,
                                       entities=_shift_entities(entities, len(caption_part)))
            return published_id, text, links
        message = await bot.send_message(chat_id, text, entities=entities or None)
        return message.message_id, text, links
    finally:
        for f in tmp_files:
            _safe_unlink(f)


async def _select_media(post_id: int) -> list[dict]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(MediaItem).where(MediaItem.post_id == post_id).order_by(MediaItem.position)
            )
        ).scalars().all()
        return [
            {"media_type": r.media_type, "local_path": r.local_path,
             "downloaded": r.downloaded, "size_bytes": r.size_bytes}
            for r in rows
        ]


NON_RETRYABLE_ERRORS = (
    "TelegramEntityTooLarge", "Request Entity Too Large", "MessageIsTooLong",
    "OversizedMedia", "медиа больше лимита", "WrongFilePart", "FileIsTooBig",
)


async def _finish_failed(bot, job_id: int, error: str, attempts: int, final: bool) -> None:
    """Непостоянные ошибки (размер файла/текста) не повторяем: сразу FAILED."""
    non_retryable = any(mark in (error or "") for mark in NON_RETRYABLE_ERRORS)
    async with session_scope() as session:
        job = await session.get(PublishJob, job_id)
        if job is None:
            return
        job.defer_reason = None
        if final or non_retryable or job.attempts >= job.max_attempts:
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
        hint = (" (повторы отключены: лимит размера — проверьте «Публикация: лимит размера медиа», "
                "сжатие видео или пропуск oversized)" if non_retryable else
                f" после {attempts} попыток")
        await _notify_owner(bot, f"⚠️ Пост #{post_id}: не удалось опубликовать{hint} — {error[:150]}")
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

    log.info("задача %s (пост %s): начинаю публикацию в @%s", job_id, post_id, channel.username)
    try:
        published_id, final_text, final_links = await _send_to_channel(bot, chat_id, post, job_id)
    except OversizedMedia as exc:
        await _finish_failed(bot, job_id, f"медиа больше лимита: {exc}", attempts, final=True)
        return
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
        post.published_text = final_text
        post.published_links = final_links
        session.add(PostEvent(
            post_id=post_id, actor=EventActor.SYSTEM, action="published",
            from_status=None, to_status=PostStatus.PUBLISHED.value,
            details={"channel": channel.username, "message_id": published_id, "attempts": attempts},
        ))
        await session.commit()
    log.info("пост %s опубликован в @%s (сообщение %s)", post_id, channel.username, published_id)
    await _notify_owner(bot, f"✅ Пост #{post_id} опубликован в @{channel.username}")
    from app.bot.cards import clear_card_keyboard
    await clear_card_keyboard(bot, post_id)
    await purge_post_media(post_id)


async def recover_in_progress_jobs() -> int:
    """Старт планировщика: возвращаем в очередь задачи, оставшиеся IN_PROGRESS.

    Их исполнитель (прошлый процесс) погиб, а _next_candidates берёт только
    QUEUED/SCHEDULED — без этого задача зависала бы навсегда.
    Задачи каналов в режиме repost не трогаем: ими владеет reader.
    """
    async with session_scope() as session:
        rows = (await session.execute(
            select(PublishJob)
            .join(TargetChannel, TargetChannel.id == PublishJob.target_channel_id)
            .where(PublishJob.state == PublishJobState.IN_PROGRESS,
                   TargetChannel.aggregate_mode != "repost"))).scalars().all()
        ids = [j.id for j in rows]
        for job in rows:
            job.state = PublishJobState.QUEUED
            job.defer_reason = None
            job.last_error = "восстановлено после перезапуска планировщика"
        if rows:
            await session.commit()
    if ids:
        log.warning("восстановлены задачи публикации после перезапуска: %s", ids)
    return len(ids)


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