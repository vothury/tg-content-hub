"""Source Reader — чтение источников + политика свежести.

Читает новые посты из подключённых источников (внешние публичные каналы и
тестовый канал-лаборатория) одним механизмом на Telethon под отдельным
аккаунтом-читателем. ТОЛЬКО чтение: никаких отправок, реакций, вступлений.

Политика свежести:
- в работу берутся посты не старше окна свежести (fresh_window_min);
- если свежих нет — последние `fallback_count` постов, но не старше
  `fallback_max_age_hours`;
- устаревшие посты не сохраняются, но курсор чтения двигается вперёд.
Параметры глобальные (.env) и на источник (sources.yaml).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from telethon import TelegramClient, errors
from telethon.tl.types import Document, PeerChannel, Photo, Video

from app.common.logging import setup_logging
from app.config import settings
from app.db.enums import EventActor, MediaType, PostStatus
from app.db.models import MediaItem, Post, PostEvent, Source
from app.db.session import session_scope
from app.services.queue import enqueue_post
from app.services.sources_sync import SourcesFileError, sync_sources
from app.services.text import make_text_hash, normalize_text

log = setup_logging("reader")

MEDIA_ROOT = Path(settings.media_dir)
if not MEDIA_ROOT.is_absolute():
    MEDIA_ROOT = Path("/app") / settings.media_dir

MAX_MESSAGES_PER_CYCLE = 200

# Ошибки, после которых продолжать опрос бессмысленно
FATAL_ERRORS = (
    errors.AuthKeyUnregisteredError,
    errors.SessionRevokedError,
    errors.UserDeactivatedBanError,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SourceSnapshot:
    id: int
    username: str | None
    telegram_id: int | None
    last_read_message_id: int | None
    poll_interval_sec: int
    backfill_limit: int
    last_read_at: datetime | None
    target_channel_id: int | None
    fresh_window_min: int
    fallback_count: int
    fallback_max_age_hours: int


# ---------- метаданные медиа ----------

def _extract_media(msg) -> tuple[MediaType | None, object | None]:
    if msg.photo is not None:
        return MediaType.PHOTO, msg.photo
    if msg.video is not None:
        return MediaType.VIDEO, msg.video
    return None, None


def _photo_dimensions(photo: Photo) -> tuple[int | None, int | None]:
    try:
        largest = max((s for s in photo.sizes if getattr(s, "w", None)), key=lambda s: s.w, default=None)
        if largest is not None:
            return getattr(largest, "w", None), getattr(largest, "h", None)
    except Exception:  # noqa: BLE001 — метаданные не критичны
        pass
    return None, None


def _video_dimensions(doc: Document) -> tuple[int | None, int | None, int | None]:
    try:
        for attr in doc.attributes:
            if isinstance(attr, Video):
                return getattr(attr, "w", None), getattr(attr, "h", None), getattr(attr, "duration", None)
    except Exception:  # noqa: BLE001
        pass
    return None, None, None


def _estimate_bytes(media: object) -> int | None:
    try:
        if isinstance(media, Photo):
            sizes = [s.size for s in media.sizes if getattr(s, "size", None)]
            return max(sizes) if sizes else None
        if isinstance(media, Document):
            return media.size
    except Exception:  # noqa: BLE001
        pass
    return None


def _media_meta(media_type: MediaType, media: object) -> dict:
    width = height = duration = None
    mime: str | None = None
    if media_type is MediaType.PHOTO:
        width, height = _photo_dimensions(media)
        mime = "image/jpeg"
    else:
        width, height, duration = _video_dimensions(media)
        mime = getattr(media, "mime_type", None)
    return {
        "src_file_id": str(getattr(media, "id", "")),
        "src_file_unique_id": str(getattr(media, "id", "")),
        "size_bytes": _estimate_bytes(media),
        "width": width,
        "height": height,
        "duration_sec": duration,
        "mime": mime,
    }


# ---------- загрузка медиа ----------

async def _download_unit_media(client, snap: SourceSnapshot, unit) -> list[dict]:
    """Скачивает медиа единицы; для каждого элемента возвращает данные строки."""
    first = unit.messages[0]
    rows: list[dict] = []
    position = 0
    size_limit = settings.max_media_download_mb * 1024 * 1024
    grouped_id = str(first.grouped_id) if first.grouped_id else None

    for msg in unit.messages:
        media_type, media = _extract_media(msg)
        if media is None:
            continue
        position += 1
        row = {
            "media_type": media_type,
            "group_id": grouped_id,
            "position": position,
            "downloaded": False,
            "local_path": None,
            "download_error": None,
            **_media_meta(media_type, media),
        }

        estimated = _estimate_bytes(media)
        if estimated is not None and estimated > size_limit:
            row["download_error"] = f"size_limit: {estimated} > {size_limit}"
            rows.append(row)
            continue

        target_dir = MEDIA_ROOT / str(snap.id) / str(first.id)
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            saved = await client.download_media(media, file=str(target_dir))
            rel = Path(saved).resolve().relative_to(MEDIA_ROOT)
            row.update(downloaded=True, local_path=str(rel), size_bytes=Path(saved).stat().st_size)
        except Exception as exc:  # noqa: BLE001 — фиксируем и идём дальше
            row["download_error"] = f"{exc.__class__.__name__}: {exc}"
        rows.append(row)
    return rows


# ---------- сохранение ----------

def _post_url(entity, message_id: int) -> str | None:
    username = getattr(entity, "username", None)
    channel_id = getattr(entity, "id", None)
    if username:
        return f"https://t.me/{username}/{message_id}"
    if channel_id:
        return f"https://t.me/c/{channel_id}/{message_id}"
    return None


async def _persist_unit(client, snap: SourceSnapshot, entity, unit) -> int | None:
    first = unit.messages[0]

    # Дедупликация до любых затрат на медиа
    async with session_scope() as session:
        exists = (
            await session.execute(
                select(Post.id).where(
                    Post.source_id == snap.id,
                    Post.source_message_id == first.id,
                )
            )
        ).first()
    if exists is not None:
        return None

    text = next((m.message for m in unit.messages if m.message), None)
    normalized = normalize_text(text)
    media_rows = await _download_unit_media(client, snap, unit)

    async with session_scope() as session:
        post = Post(
            source_id=snap.id,
            source_message_id=first.id,
            target_channel_id=snap.target_channel_id,
            post_url=_post_url(entity, first.id),
            original_text=text,
            normalized_text=normalized,
            text_hash=make_text_hash(normalized),
            status=PostStatus.NEW,
        )
        session.add(post)
        await session.flush()
        for row in media_rows:
            session.add(MediaItem(post_id=post.id, **row))
        session.add(
            PostEvent(post_id=post.id, actor=EventActor.SYSTEM, action="created", to_status=PostStatus.NEW.value)
        )
        await session.commit()
        await session.refresh(post)
        return post.id


# ---------- работа с источниками ----------

async def load_sources() -> list[SourceSnapshot]:
    async with session_scope() as session:
        rows = (await session.execute(select(Source).where(Source.enabled.is_(True)))).scalars().all()
        return [
            SourceSnapshot(
                id=r.id,
                username=r.username,
                telegram_id=r.telegram_id,
                last_read_message_id=r.last_read_message_id,
                poll_interval_sec=r.poll_interval_sec or settings.reader_default_source_interval_sec,
                backfill_limit=r.backfill_limit or settings.reader_backfill_limit,
                last_read_at=r.last_read_at,
                target_channel_id=r.target_channel_id,
                fresh_window_min=settings.reader_fresh_window_min if r.fresh_window_min is None else r.fresh_window_min,
                fallback_count=settings.reader_fallback_count if r.fallback_count is None else r.fallback_count,
                fallback_max_age_hours=settings.reader_fallback_max_age_hours if r.fallback_max_age_hours is None else r.fallback_max_age_hours,
            )
            for r in rows
        ]


async def resolve_entity(client: TelegramClient, snap: SourceSnapshot):
    if snap.telegram_id:
        return await client.get_entity(PeerChannel(snap.telegram_id))
    if snap.username:
        return await client.get_entity(snap.username)
    raise ValueError(f"источник #{snap.id} без username и telegram_id")


async def sync_source_meta(snap: SourceSnapshot, entity) -> None:
    title = getattr(entity, "title", None)
    async with session_scope() as session:
        source = await session.get(Source, snap.id)
        if source is None:
            return
        changed = False
        if entity.id and source.telegram_id != entity.id:
            source.telegram_id = entity.id; changed = True
        if title and source.title != title:
            source.title = title; changed = True
        if changed:
            await session.commit()


def build_units(messages: list) -> list:
    """Разбирает сообщения на единицы; альбомы склеиваются по grouped_id."""
    units = []
    groups: dict[int, object] = {}
    for msg in messages:
        if getattr(msg, "action", None) is not None:
            continue  # сервисные сообщения не обрабатываем
        if msg.grouped_id:
            unit = groups.get(msg.grouped_id)
            if unit is None:
                unit = type("Unit", (), {"messages": []})()
                groups[msg.grouped_id] = unit
                units.append(unit)
            unit.messages.append(msg)
        else:
            units.append(type("Unit", (), {"messages": [msg]})())
    return units


def _select_fresh(messages: list, snap: SourceSnapshot) -> list:
    """Политика свежести: окно свежести; если пусто — фолбэк последних."""
    now = _utcnow()
    fresh_limit = timedelta(minutes=snap.fresh_window_min)
    fallback_limit = timedelta(hours=snap.fallback_max_age_hours)

    fresh = [m for m in messages if m.date is not None and (now - m.date) <= fresh_limit]
    if fresh:
        return fresh
    recent = [m for m in messages if m.date is not None and (now - m.date) <= fallback_limit]
    if snap.fallback_count > 0:
        return recent[-snap.fallback_count:]
    return []


async def mark_read(snap: SourceSnapshot, last_id: int | None) -> None:
    async with session_scope() as session:
        source = await session.get(Source, snap.id)
        if source is None:
            return
        if last_id is not None:
            source.last_read_message_id = max(source.last_read_message_id or 0, last_id)
        source.last_read_at = _utcnow()
        await session.commit()


async def process_source(client: TelegramClient, snap: SourceSnapshot) -> None:
    if snap.last_read_at is not None:
        elapsed = (_utcnow() - snap.last_read_at).total_seconds()
        if elapsed < snap.poll_interval_sec:
            return

    entity = await resolve_entity(client, snap)
    await sync_source_meta(snap, entity)

    if snap.last_read_message_id is None:
        fetch_limit = max(snap.backfill_limit, snap.fallback_count, 1)
        messages = await client.get_messages(entity, limit=fetch_limit)
    else:
        messages = await client.get_messages(
            entity, min_id=snap.last_read_message_id, limit=MAX_MESSAGES_PER_CYCLE
        )
    messages = sorted(messages, key=lambda m: m.id)

    if not messages:
        await mark_read(snap, None)
        return

    selected = _select_fresh(messages, snap)
    skipped = len(messages) - len(selected)
    if skipped:
        log.info("источник #%s: пропущено устаревших постов: %d", snap.id, skipped)

    if selected:
        created = 0
        for unit in build_units(selected):
            post_id = await _persist_unit(client, snap, entity, unit)
            if post_id is not None:
                created += 1
                await enqueue_post(post_id)
        log.info(
            "источник #%s (%s): прочитано %d, в работу %d, создано постов %d",
            snap.id, snap.username or snap.telegram_id, len(messages), len(selected), created,
        )
    else:
        log.info("источник #%s: новых подходящих постов нет", snap.id)

    # курсор двигаем по всем прочитанным, включая устаревшие
    await mark_read(snap, max(m.id for m in messages))


async def _idle_forever(reason: str) -> None:
    while True:
        log.warning("reader в простое: %s", reason)
        await asyncio.sleep(600)


async def main() -> None:
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        await _idle_forever("TELEGRAM_API_ID/API_HASH не заданы в .env")

    client = TelegramClient(
        settings.reader_session_path,
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    await client.connect()

    if not await client.is_user_authorized():
        await client.disconnect()
        await _idle_forever("сессия не авторизована — выполните `make login` и перезапустите reader")

    me = await client.get_me()
    log.info("reader запущен; аккаунт-читатель: id=%s username=%s", me.id, me.username)

    # Декларативные источники: при наличии файла он — источник истины
    try:
        await sync_sources()
    except SourcesFileError as exc:
        log.error("ошибка в sources.yaml: %s — продолжаю с источниками из БД", exc)

    try:
        while True:
            try:
                snapshots = await load_sources()
            except Exception:  # noqa: BLE001
                log.exception("не удалось загрузить список источников")
                snapshots = []

            for snap in snapshots:
                try:
                    await process_source(client, snap)
                except errors.FloodWaitError as exc:
                    delay = min(int(getattr(exc, "seconds", 30)) + 5, 300)
                    log.warning("FloodWait %s сек у источника #%s — пауза", delay, snap.id)
                    await asyncio.sleep(delay)
                except FATAL_ERRORS:
                    log.exception("фатальная ошибка аккаунта-читателя")
                    await client.disconnect()
                    await _idle_forever("аккаунт-читатель недоступен (см. трейсбек)")
                except Exception:  # noqa: BLE001
                    log.exception("ошибка обработки источника #%s", snap.id)
                await asyncio.sleep(2)  # щадящая пауза между источниками

            await asyncio.sleep(settings.reader_poll_interval_sec)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())