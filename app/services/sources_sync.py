"""Синхронизация источников из декларативного sources.yaml в таблицу sources.

Файл — единое наглядное место управления источниками. Отсутствие файла —
синхронизация не выполняется, база не трогается. Источник, пропавший из
списка, отключается (мягко), но не удаляется: посты и медиа сохраняются.
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml
from sqlalchemy import select

from app.db.enums import SourceKind
from app.db.models import Source
from app.db.session import session_scope

log = logging.getLogger(__name__)

SOURCES_FILE = Path("sources.yaml")


class SourcesFileError(Exception):
    """Файл источников некорректен."""


def load_sources_file(path: Path = SOURCES_FILE) -> list[dict] | None:
    """Возвращает нормализованный список источников или None, если файла/списка нет."""
    if not path.exists():
        return None
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourcesFileError(f"не удалось прочитать {path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        raise SourcesFileError(f"ошибка синтаксиса YAML в {path}: {exc}") from exc

    entries = data.get("sources")
    if not isinstance(entries, list):
        log.warning("%s: список 'sources' отсутствует — синхронизация пропущена", path)
        return None

    normalized: list[dict] = []
    for i, raw in enumerate(entries, start=1):
        if not isinstance(raw, dict) or not raw.get("username"):
            raise SourcesFileError(f"{path}: запись №{i} — обязательно поле 'username'")
        kind_raw = str(raw.get("kind", "external")).strip().lower()
        try:
            kind = SourceKind(kind_raw)
        except ValueError:
            raise SourcesFileError(f"{path}: запись №{i} — kind должен быть test|external, получено '{kind_raw}'")
        normalized.append({
            "username": str(raw["username"]).lstrip("@").strip(),
            "kind": kind,
            "enabled": bool(raw.get("enabled", True)),
            "poll_interval_sec": raw.get("poll_interval_sec"),
            "backfill_limit": raw.get("backfill_limit"),
            "filters": raw.get("filters"),
        })
    return normalized


async def sync_sources(path: Path = SOURCES_FILE) -> None:
    """Примиряет таблицу sources с файлом."""
    entries = load_sources_file(path)
    if entries is None:
        return

    listed = {e["username"] for e in entries}
    created = updated = disabled = 0

    async with session_scope() as session:
        existing = {
            s.username: s
            for s in (await session.execute(select(Source))).scalars().all()
        }

        for e in entries:
            src = existing.get(e["username"])
            if src is None:
                session.add(Source(
                    username=e["username"],
                    title=e["username"],
                    kind=e["kind"],
                    enabled=e["enabled"],
                    poll_interval_sec=e["poll_interval_sec"],
                    backfill_limit=e["backfill_limit"],
                    filters=e["filters"],
                ))
                created += 1
                continue
            changed = False
            if src.kind is not e["kind"]:
                src.kind = e["kind"]; changed = True
            if src.enabled != e["enabled"]:
                src.enabled = e["enabled"]; changed = True
            if src.poll_interval_sec != e["poll_interval_sec"]:
                src.poll_interval_sec = e["poll_interval_sec"]; changed = True
            if src.backfill_limit != e["backfill_limit"]:
                src.backfill_limit = e["backfill_limit"]; changed = True
            if src.filters != e["filters"]:
                src.filters = e["filters"]; changed = True
            if changed:
                updated += 1

        # Пропавшие из файла — мягко отключаем, данные сохраняем
        for username, src in existing.items():
            if username not in listed and src.enabled:
                src.enabled = False
                disabled += 1
                log.warning("источник @%s отсутствует в %s — отключён (посты сохранены)", username, path.name)

        await session.commit()

    log.info("синхронизация источников: добавлено %d, обновлено %d, отключено %d", created, updated, disabled)