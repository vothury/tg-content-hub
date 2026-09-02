"""Черновик карты контента, провенанс и условный sync по хешу (6c.2)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.db.models import AppSetting
from app.db.session import session_scope
from app.services import sources_sync
from app.services.settings import get_setting

K_ORIGIN = "config.origin"
K_UPDATED = "config.updated_at"
K_FILE_HASH = "config.file_hash"
K_DRAFT = "config.draft"


async def _set(session, key, value):
    row = (await session.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value


def _file_path() -> Path:
    """sources.yaml с учётом рабочей директории контейнера."""
    p = sources_sync.DEFAULT_PATH
    if p.is_absolute():
        return p
    for base in (Path.cwd(), Path("/app")):
        cand = base / p.name
        if cand.exists():
            return cand
    return Path.cwd() / p.name


def read_file_text() -> str | None:
    p = _file_path()
    return p.read_text(encoding="utf-8") if p.exists() else None


async def get_meta() -> dict:
    async with session_scope() as session:
        origin = await get_setting(session, K_ORIGIN)
        updated = await get_setting(session, K_UPDATED)
        fhash = await get_setting(session, K_FILE_HASH)
        draft = await get_setting(session, K_DRAFT)
    file_text = read_file_text() or ""
    return {
        "origin": origin or "file",
        "updated_at": (updated or "")[:16].replace("T", " "),
        "draft": draft,
        "file_text": file_text,
        "file_found": bool(file_text),
        "file_changed": bool(file_text and fhash and sources_sync.file_hash(file_text) != fhash),
    }


async def save_draft(text: str) -> None:
    async with session_scope() as session:
        await _set(session, K_DRAFT, text)
        await session.commit()


async def web_apply(text: str) -> dict:
    stats = await sources_sync.apply_sources_text(text)
    async with session_scope() as session:
        await _set(session, K_ORIGIN, "web")
        await _set(session, K_UPDATED, datetime.now(timezone.utc).isoformat())
        await _set(session, K_DRAFT, text)
        await session.commit()
    return stats


async def file_sync_conditional():
    text = read_file_text()
    if text is None:
        return None
    h = sources_sync.file_hash(text)
    async with session_scope() as session:
        stored = await get_setting(session, K_FILE_HASH)
    if stored == h:
        return None  # файл не менялся — веб-правки не перетираем
    stats = await sources_sync.apply_sources_text(text)
    async with session_scope() as session:
        await _set(session, K_ORIGIN, "file")
        await _set(session, K_UPDATED, datetime.now(timezone.utc).isoformat())
        await _set(session, K_FILE_HASH, h)
        await session.commit()
    return stats