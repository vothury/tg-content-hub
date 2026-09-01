"""Синхронизация sources.yaml (целевые каналы + источники) в базу.

Файл — единая карта контента: какие источники питают какие целевые каналы.
Файла нет — синхронизация не выполняется, база не трогается.
Источник, пропавший из списка, отключается (мягко), но не удаляется.
Целевые каналы из файла не удаляются — только создаются/обновляются.
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml
from sqlalchemy import select

from app.db.enums import SourceKind
from app.db.models import Source, TargetChannel
from app.db.session import session_scope

log = logging.getLogger(__name__)

SOURCES_FILE = Path("sources.yaml")


class SourcesFileError(Exception):
    """Файл каналов/источников некорректен."""


def _norm_username(value) -> str:
    return str(value).lstrip("@").strip()


def _parse_relevance(value, index: int) -> int | None:
    if value is None:
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise SourcesFileError(f"запись sources №{index}: relevance должен быть целым 1-10")
    if not 1 <= v <= 10:
        raise SourcesFileError(f"запись sources №{index}: relevance должен быть в пределах 1-10")
    return v


def load_sources_file(path: Path = SOURCES_FILE):
    """Возвращает (targets, sources) или None, если файла/списка источников нет."""
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

    if not isinstance(data, dict):
        raise SourcesFileError(f"{path}: верхний уровень должен быть словарём с блоками targets и sources")

    targets_raw = data.get("targets") or []
    sources_raw = data.get("sources")
    if not isinstance(targets_raw, list):
        raise SourcesFileError(f"{path}: блок 'targets' должен быть списком")
    if not isinstance(sources_raw, list):
        log.warning("%s: блок 'sources' отсутствует — источники не синхронизируются", path)
        sources_raw = []

    targets: list[dict] = []
    for i, raw in enumerate(targets_raw, start=1):
        if not isinstance(raw, dict) or not raw.get("username"):
            raise SourcesFileError(f"{path}: запись targets №{i} — обязательно поле 'username'")
        targets.append({
            "username": _norm_username(raw["username"]),
            "title": str(raw.get("title") or raw["username"]),
            "daily_limit": raw.get("daily_limit"),
            "min_interval_min": raw.get("min_interval_min"),
            "quiet_hours": raw.get("quiet_hours"),
            "description": raw.get("description"),
            "rewrite": raw.get("rewrite"),
        })

    sources: list[dict] = []
    for i, raw in enumerate(sources_raw, start=1):
        if not isinstance(raw, dict) or not raw.get("username"):
            raise SourcesFileError(f"{path}: запись sources №{i} — обязательно поле 'username'")
        kind_raw = str(raw.get("kind", "external")).strip().lower()
        try:
            kind = SourceKind(kind_raw)
        except ValueError:
            raise SourcesFileError(f"{path}: запись sources №{i} — kind должен быть test|external, получено '{kind_raw}'")
        sources.append({
            "username": _norm_username(raw["username"]),
            "kind": kind,
            "enabled": bool(raw.get("enabled", True)),
            "poll_interval_sec": raw.get("poll_interval_sec"),
            "backfill_limit": raw.get("backfill_limit"),
            "filters": raw.get("filters"),
            "target": _norm_username(raw["target"]) if raw.get("target") else None,
            "fresh_window_min": raw.get("fresh_window_min"),
            "fallback_count": raw.get("fallback_count"),
            "fallback_max_age_hours": raw.get("fallback_max_age_hours"),
            "relevance": _parse_relevance(raw.get("relevance"), i),
        })
    return targets, sources


async def sync_sources(path: Path = SOURCES_FILE) -> None:
    """Примиряет target_channels и sources с файлом."""
    parsed = load_sources_file(path)
    if parsed is None:
        return
    targets_cfg, sources_cfg = parsed

    async with session_scope() as session:
        # --- целевые каналы: только создание и обновление ---
        existing_targets = {
            t.username: t for t in (await session.execute(select(TargetChannel))).scalars().all()
        }
        t_created = t_updated = 0
        for cfg in targets_cfg:
            ch = existing_targets.get(cfg["username"])
            if ch is None:
                session.add(TargetChannel(
                    username=cfg["username"],
                    title=cfg["title"],
                    description=cfg["description"],
                    daily_limit=cfg["daily_limit"] or 6,
                    min_interval_min=cfg["min_interval_min"] or 60,
                    quiet_hours=cfg["quiet_hours"],
                    rewrite_enabled=True if cfg["rewrite"] is None else bool(cfg["rewrite"]),  # ← добавить
                ))
                t_created += 1
                continue
            changed = False
            if ch.title != cfg["title"]:
                ch.title = cfg["title"]; changed = True
            if cfg["daily_limit"] is not None and ch.daily_limit != int(cfg["daily_limit"]):
                ch.daily_limit = int(cfg["daily_limit"]); changed = True
            if cfg["min_interval_min"] is not None and ch.min_interval_min != int(cfg["min_interval_min"]):
                ch.min_interval_min = int(cfg["min_interval_min"]); changed = True
            if cfg["quiet_hours"] is not None and ch.quiet_hours != cfg["quiet_hours"]:
                ch.quiet_hours = cfg["quiet_hours"]; changed = True
            if cfg["description"] is not None and ch.description != cfg["description"]:
                ch.description = cfg["description"]; changed = True
            rw = True if cfg["rewrite"] is None else bool(cfg["rewrite"])
            if ch.rewrite_enabled != rw:
                ch.rewrite_enabled = rw; changed = True
            if changed:
                t_updated += 1
        await session.flush()

        target_ids = {
            t.username: t.id for t in (await session.execute(select(TargetChannel))).scalars().all()
        }

        # --- источники ---
        listed = {e["username"] for e in sources_cfg}
        existing_sources = {
            s.username: s for s in (await session.execute(select(Source))).scalars().all()
        }
        s_created = s_updated = s_disabled = 0
        for e in sources_cfg:
            target_id = None
            if e["target"]:
                target_id = target_ids.get(e["target"])
                if target_id is None:
                    raise SourcesFileError(
                        f"источник @{e['username']}: целевой канал @{e['target']} не найден — "
                        "добавьте его в блок targets"
                    )
            src = existing_sources.get(e["username"])
            if src is None:
                session.add(Source(
                    username=e["username"],
                    title=e["username"],
                    kind=e["kind"],
                    enabled=e["enabled"],
                    poll_interval_sec=e["poll_interval_sec"],
                    backfill_limit=e["backfill_limit"],
                    filters=e["filters"],
                    target_channel_id=target_id,
                    fresh_window_min=e["fresh_window_min"],
                    fallback_count=e["fallback_count"],
                    fallback_max_age_hours=e["fallback_max_age_hours"],
                    relevance=e["relevance"],
                ))
                s_created += 1
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
            if src.fresh_window_min != e["fresh_window_min"]:
                src.fresh_window_min = e["fresh_window_min"]; changed = True
            if src.fallback_count != e["fallback_count"]:
                src.fallback_count = e["fallback_count"]; changed = True
            if src.fallback_max_age_hours != e["fallback_max_age_hours"]:
                src.fallback_max_age_hours = e["fallback_max_age_hours"]; changed = True
            if src.target_channel_id != target_id:
                src.target_channel_id = target_id; changed = True
            if src.relevance != e["relevance"]:
                src.relevance = e["relevance"]; changed = True
            if changed:
                s_updated += 1

        # Пропавшие из файла источники — мягко отключаем
        for username, src in existing_sources.items():
            if username not in listed and src.enabled:
                src.enabled = False
                s_disabled += 1
                log.warning("источник @%s отсутствует в %s — отключён (посты сохранены)", username, path.name)

        await session.commit()

    log.info(
        "синхронизация: целевых каналов +%d/~%d; источников +%d/~%d, отключено %d",
        t_created, t_updated, s_created, s_updated, s_disabled,
    )