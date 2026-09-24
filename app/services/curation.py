"""Курирование: приём пересылок из канала-приёмника с указанием целевых каналов.

Владелец пересылает понравившийся пост в один из каналов-приёмников (настройка
curation_inbox_channels) и добавляет подпись с именами целевых каналов
(@testim_foto или testim_foto; несколько — через пробел/запятую/перенос).
Система находит ОРИГИНАЛ пересылки, скачивает его медиа и создаёт по одному посту
на каждый указанный целевой канал (curated=true, target_channel_id фиксирован —
публикация возможна только туда). Обрабатываются только каналы из настройки.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import select
from telethon.tl.types import PeerChannel

from app.db.enums import EventActor, PostStatus
from app.db.models import MediaItem, Post, PostEvent, Source, TargetChannel
from app.db.session import session_scope
from app.services.settings import Keys, get_setting
from app.services.text import html_to_text, make_text_hash, normalize_text

log = logging.getLogger("curation")

_TOKEN_RE = re.compile(r"@?[a-zA-Z0-9_]{4,}")


async def _enabled() -> bool:
    async with session_scope() as session:
        v = await get_setting(session, Keys.CURATION_ENABLED)
    return True if v is None else bool(int(v))


async def _inbox_channels() -> list:
    async with session_scope() as session:
        v = await get_setting(session, Keys.CURATION_INBOX_CHANNELS)
    if isinstance(v, str):
        v = [x.strip() for x in v.split(",") if x.strip()]
    return [str(x).strip().lstrip("@").lower() for x in (v or []) if str(x).strip()]


async def _target_usernames() -> dict:
    async with session_scope() as session:
        rows = (await session.execute(
            select(TargetChannel.id, TargetChannel.username))).all()
    return {(u or "").strip().lower(): i for i, u in rows if u}


def parse_targets(caption, known: set) -> list:
    """Целевые каналы из подписи пересылки; порядок сохранён, дубли убраны."""
    if not caption:
        return []
    out = []
    for tok in _TOKEN_RE.findall(caption):
        n = tok.lstrip("@").lower()
        if n in known and n not in out:
            out.append(n)
    return out


async def _ensure_source(username: str, telegram_id, title) -> int:
    async with session_scope() as session:
        row = (await session.execute(
            select(Source).where(Source.username == username))).scalar_one_or_none()
        if row is not None:
            if not row.manual:
                row.manual = True
                await session.commit()
            return row.id
        row = Source(username=username, telegram_id=telegram_id,
                     title=title or f"manual:{username}",
                     enabled=False, manual=True, poll_interval_sec=0, backfill_limit=0)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id


async def _origin_messages(client, msg):
    """(entity, [сообщения оригинала]) для пересылки, включая альбомы."""
    fwd = getattr(msg, "forward", None) or getattr(msg, "fwd_from", None)
    if fwd is None:
        return None, []
    ch_id = getattr(fwd, "channel_id", None) \
        or getattr(getattr(fwd, "from_id", None), "channel_id", None)
    post_id = getattr(fwd, "channel_post", None) or getattr(fwd, "msg_id", None)
    if not ch_id or not post_id:
        return None, []
    try:
        entity = await client.get_entity(PeerChannel(int(ch_id)))
        first = [m for m in (await client.get_messages(entity, ids=[int(post_id)])) if m]
        if not first:
            return None, []
        gid = first[0].grouped_id
        if gid:
            around = await client.get_messages(
                entity, min_id=max(1, int(post_id) - 9), max_id=int(post_id) + 9, limit=20)
            group = [m for m in around if m.grouped_id == gid]
            return entity, (group or first)
        return entity, first
    except Exception:  # noqa: BLE001
        log.exception("курирование: не удалось достать оригинал пересылки")
        return None, []


async def process_inboxes(client) -> None:
    if not await _enabled():
        return
    inboxes = await _inbox_channels()
    targets = await _target_usernames()
    if not inboxes or not targets:
        return
    from app.workers import reader as R  # локально: reader импортирует curation
    for inbox in inboxes:
        try:
            await _process_one_inbox(client, R, inbox, targets)
        except Exception:  # noqa: BLE001
            log.exception("курирование: сбой обработчика приёмника %s", inbox)


async def _process_one_inbox(client, R, inbox: str, targets: dict) -> None:
    entity = await client.get_entity(inbox)
    inbox_src_id = await _ensure_source(inbox, getattr(entity, "id", None),
                                        getattr(entity, "title", None))
    async with session_scope() as session:
        src = await session.get(Source, inbox_src_id)
        cursor = src.last_read_message_id if src is not None else None

    messages = sorted(await client.get_messages(entity, min_id=cursor or 0, limit=50),
                      key=lambda m: m.id)
    if not messages:
        return

    for msg in messages:
        fwd = getattr(msg, "forward", None) or getattr(msg, "fwd_from", None)
        caption = getattr(msg, "message", None) or getattr(msg, "text", None)
        tg = parse_targets(caption, set(targets))
        if fwd is None or not tg:
            continue  # не пересылка или подпись без известных целевых каналов

        origin_entity, orig_msgs = await _origin_messages(client, msg)
        if not orig_msgs:
            log.warning("курирование: оригинал пересылки в %s не найден — пропуск", inbox)
            continue

        origin_username = getattr(origin_entity, "username", None) or str(getattr(origin_entity, "id", ""))
        origin_src_id = await _ensure_source(origin_username, getattr(origin_entity, "id", None),
                                             getattr(origin_entity, "title", None))
        snap = R.SourceSnapshot(
            id=origin_src_id, username=getattr(origin_entity, "username", None),
            telegram_id=getattr(origin_entity, "id", None), last_read_message_id=None,
            poll_interval_sec=0, backfill_limit=0, last_read_at=None, target_channel_id=None,
            fresh_window_min=0, fallback_count=0, fallback_max_age_hours=0)
        unit = type("Unit", (), {"messages": orig_msgs})()
        media_rows = await R._download_unit_media(client, snap, unit)

        first = orig_msgs[0]
        raw = next((m.message for m in orig_msgs if m.message), None)
        ent_msg = next((m for m in orig_msgs if m.message), None)
        text = R._annotate_links(raw, getattr(ent_msg, "entities", None)) if raw else None
        text = html_to_text(text)
        normalized = normalize_text(text)
        thash = make_text_hash(normalized)
        origin_url = R._post_url(origin_entity, first.id)

        created = []
        for tgt in tg:
            ch_id = targets.get(tgt)
            if ch_id is None:
                continue
            async with session_scope() as session:
                dup = (await session.execute(
                    select(Post.id).where(
                        Post.source_id == origin_src_id,
                        Post.source_message_id == first.id,
                        Post.target_channel_id == ch_id).limit(1))).scalar_one_or_none()
                if dup is not None:
                    continue
                post = Post(source_id=origin_src_id, source_message_id=first.id,
                            target_channel_id=ch_id, post_url=origin_url,
                            original_text=text, normalized_text=normalized, text_hash=thash,
                            status=PostStatus.NEW, source_published_at=first.date, curated=True)
                session.add(post)
                await session.flush()
                for row in media_rows:
                    session.add(MediaItem(post_id=post.id, **row))
                session.add(PostEvent(post_id=post.id, actor=EventActor.OWNER, action="curated",
                                      to_status=PostStatus.NEW.value,
                                      details={"inbox": inbox, "origin": origin_url, "target": tgt}))
                await session.commit()
                await session.refresh(post)
                created.append(post.id)
        for pid in created:
            await R.enqueue_post(pid)
        if created:
            log.info("курирование: %s -> посты %s (цели: %s)", inbox, created, tg)

    async with session_scope() as session:
        src = await session.get(Source, inbox_src_id)
        if src is not None:
            src.last_read_message_id = max(src.last_read_message_id or 0,
                                           max(m.id for m in messages))
            await session.commit()