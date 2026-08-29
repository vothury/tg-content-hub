"""Source Reader — Этап 1.

Читает новые посты из подключённых источников (внешние публичные каналы и
тестовый канал-лаборатория) одним механизмом на Telethon под отдельным
аккаунтом-читателем. ТОЛЬКО чтение: никаких отправок, реакций, вступлений.

Поведение:
- цикл с шагом READER_POLL_INTERVAL_SEC;
- у каждого источника свой интервал опроса (по умолчанию 120 сек);
- первый опрос источника — ограниченный бэкфил (READER_BACKFILL_LIMIT);
- посты сохраняются с дедупликацией (source_id, source_message_id);
- медиа скачиваются в локальный том (лимит размера — предохранитель);
- каждый новый пост ставится в очередь пайплайна в Redis.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from telethon import TelegramClient, errors
from telethon.tl.types import Document, DocumentAttributeVideo, PeerChannel, Photo

from app.common.logging import setup_logging
from app.config import settings
from app.db.enums import EventActor, MediaType, PostStatus
from app.db.models import MediaItem, Post, PostEvent, Source
from app.db.session import session_scope
from app.services.queue import enqueue_post
from app.services.text import make_text_hash, normalize_text
from app.services.sources_sync import SourcesFileError, sync_sources

log = setup_logging("reader")

MEDIA_ROOT = Path(settings.media_dir)
if not MEDIA_ROOT.is_absolute():
    MEDIA_ROOT = Path("/app") / settings.media_dir

MAX_MESSAGES_PER_CYCLE = 200

# После этих ошибок продолжать опрос бессмысленно — ждём вмешательства
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


@dataclass
class Unit:
    """Единица обработки: одиночный пост или альбом (группа сообщений)."""

    messages: list


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
            if isinstance(attr, DocumentAttributeVideo):
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

async def _download_unit_media(client, snap: SourceSnapshot, unit: Unit) -> list[dict]:
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


async def _persist_unit(client, snap: SourceSnapshot, entity, unit: Unit) -> int | None:
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
    if entity.id == snap.telegram_id and title == snap_title_get(snap):
        return
    async with session_scope() as session:
        source = await session.get(Source, snap.id)
        if source is None:
            return
        if entity.id and source.telegram_id != entity.id:
            source.telegram_id = entity.id
        if title and source.title != title:
            source.title = title
        await session.commit()


def snap_title_get(snap: SourceSnapshot):  # вспомогательная, чтобы не дёргать БД
    return None


def build_units(messages: list) -> list[Unit]:
    """Разбирает сообщения на единицы; альбомы склеиваются по grouped_id."""
    units: list[Unit] = []
    groups: dict[int, Unit] = {}
    for msg in messages:
        if getattr(msg, "action", None) is not None:
            continue  # сервисные сообщения не обрабатываем
        if msg.grouped_id:
            unit = groups.get(msg.grouped_id)
            if unit is None:
                unit = Unit(messages=[])
                groups[msg.grouped_id] = unit
                units.append(unit)
            unit.messages.append(msg)
        else:
            units.append(Unit(messages=[msg]))
    return units


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
        messages = await client.get_messages(entity, limit=snap.backfill_limit)
    else:
        messages = await client.get_messages(
            entity, min_id=snap.last_read_message_id, limit=MAX_MESSAGES_PER_CYCLE
        )
    messages = sorted(messages, key=lambda m: m.id)

    if not messages:
        await mark_read(snap, None)
        return

    created = 0
    for unit in build_units(messages):
        post_id = await _persist_unit(client, snap, entity, unit)
        if post_id is not None:
            created += 1
            await enqueue_post(post_id)

    await mark_read(snap, max(m.id for m in messages))
    log.info(
        "источник #%s (%s): прочитано сообщений %d, создано постов %d",
        snap.id, snap.username or snap.telegram_id, len(messages), created,
    )


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
    log.info("reader запущен (Этап 1); аккаунт-читатель: id=%s username=%s", me.id, me.username)

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