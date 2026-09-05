"""Карта контента (sources.yaml) -> БД. Файл — источник правды.
6c.2: применение из строки (веб) + условная синхронизация по хешу."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import yaml
from sqlalchemy import select

from app.config import settings
from app.db.enums import SourceKind
from app.db.models import Source, StyleProfile, TargetChannel
from app.db.session import session_scope

log = logging.getLogger("sources_sync")

DEFAULT_PATH = Path("sources.yaml")


class SourcesFileError(Exception):
    pass


def _norm_username(value) -> str:
    s = str(value).strip()
    return s[1:] if s.startswith("@") else s


def _parse_relevance(value, index: int):
    if value is None:
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise SourcesFileError(f"sources №{index}: relevance должен быть целым 1-10")
    if not 1 <= v <= 10:
        raise SourcesFileError(f"sources №{index}: relevance в пределах 1-10")
    return v


def file_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_sources_text(text: str):
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise SourcesFileError(f"некорректный YAML: {exc}")

    styles = []
    for i, s in enumerate(raw.get("styles") or [], 1):
        name = str(s.get("name") or "").strip()
        if not name:
            raise SourcesFileError(f"styles №{i}: обязательно name")
        styles.append({
            "name": name,
            "rewrite_prompt": str(s.get("rewrite_prompt") or "").strip() or None,
            "preserve_source_tone": bool(s.get("preserve_source_tone", False)),
        })

    targets = []
    for i, t in enumerate(raw.get("targets") or [], 1):
        if not t.get("username"):
            raise SourcesFileError(f"targets №{i}: обязательно username")
        targets.append({
            "username": _norm_username(t["username"]),
            "title": str(t.get("title") or t["username"]),
            "description": t.get("description"),
            "daily_limit": t.get("daily_limit"),
            "min_interval_min": t.get("min_interval_min"),
            "quiet_hours": t.get("quiet_hours"),
            "rewrite": t.get("rewrite"),
            "style": str(t.get("style") or "").strip() or None,
            "autopilot": t.get("autopilot"),
            "autopilot_min_score": t.get("autopilot_min_score"),
            "review_if_uncertain": t.get("review_if_uncertain"),
            "double_check": t.get("double_check"),
        })

    sources = []
    for i, s in enumerate(raw.get("sources") or [], 1):
        if not s.get("username"):
            raise SourcesFileError(f"sources №{i}: обязательно username")
        try:
            kind = SourceKind(str(s.get("kind") or "external"))
        except ValueError:
            raise SourcesFileError(f"sources №{i}: kind должен быть test|external")
        f = s.get("filters") or {}
        sources.append({
            "username": _norm_username(s["username"]),
            "kind": kind,
            "target": _norm_username(s["target"]) if s.get("target") else None,
            "enabled": bool(s.get("enabled", True)),
            "poll_interval_sec": s.get("poll_interval_sec"),
            "fresh_window_min": s.get("fresh_window_min"),
            "fallback_count": s.get("fallback_count"),
            "fallback_max_age_hours": s.get("fallback_max_age_hours"),
            "relevance": _parse_relevance(s.get("relevance"), i),
            "filters": {"min_text_len": f.get("min_text_len"), "blacklist_words": f.get("blacklist_words")},
        })
    return targets, sources, styles


def load_sources_file(path=DEFAULT_PATH):
    p = Path(path)
    if not p.exists():
        return None
    return parse_sources_text(p.read_text(encoding="utf-8"))


async def apply_parsed(parsed) -> dict:
    targets_cfg, sources_cfg, styles_cfg = parsed
    stats = {"styles": [0, 0], "targets": [0, 0], "sources": [0, 0], "disabled": 0}
    d_interval = getattr(settings, "reader_default_source_interval_sec", 300)
    d_window = getattr(settings, "reader_fresh_window_min", 60)
    d_fb = getattr(settings, "reader_fallback_count", 2)
    d_fb_h = getattr(settings, "reader_fallback_max_age_hours", 48)
    async with session_scope() as session:
        for e in styles_cfg:
            sp = (await session.execute(select(StyleProfile).where(StyleProfile.name == e["name"]))).scalar_one_or_none()
            if sp is None:
                session.add(StyleProfile(name=e["name"], rewrite_prompt=e["rewrite_prompt"],
                                         preserve_source_tone=e["preserve_source_tone"], version=1, is_active=True))
                stats["styles"][0] += 1
            elif (sp.rewrite_prompt or None) != e["rewrite_prompt"] or sp.preserve_source_tone != e["preserve_source_tone"]:
                sp.rewrite_prompt = e["rewrite_prompt"]; sp.preserve_source_tone = e["preserve_source_tone"]
                stats["styles"][1] += 1
        await session.flush()
        style_ids = {sp.name: sp.id for sp in (await session.execute(select(StyleProfile))).scalars().all()}

        existing = {c.username: c for c in (await session.execute(select(TargetChannel))).scalars().all()}
        for cfg in targets_cfg:
            style_id = style_ids.get(cfg["style"])
            ch = existing.get(cfg["username"])
            if ch is None:
                session.add(TargetChannel(username=cfg["username"], title=cfg["title"], description=cfg["description"],
                                          daily_limit=cfg["daily_limit"] or 6, min_interval_min=cfg["min_interval_min"] or 60,
                                          quiet_hours=cfg["quiet_hours"],
                                          rewrite_enabled=True if cfg["rewrite"] is None else bool(cfg["rewrite"]),
                                          autopilot=bool(cfg["autopilot"]),
                                          autopilot_min_score=cfg["autopilot_min_score"],
                                          review_if_uncertain=True if cfg["review_if_uncertain"] is None else bool(cfg["review_if_uncertain"]),
                                          double_check=bool(cfg["double_check"]),
                                          style_profile_id=style_id))
                stats["targets"][0] += 1
                continue
            changed = False
            if ch.title != cfg["title"]: ch.title = cfg["title"]; changed = True
            if cfg["description"] is not None and ch.description != cfg["description"]: ch.description = cfg["description"]; changed = True
            if cfg["daily_limit"] and ch.daily_limit != cfg["daily_limit"]: ch.daily_limit = cfg["daily_limit"]; changed = True
            if cfg["min_interval_min"] and ch.min_interval_min != cfg["min_interval_min"]: ch.min_interval_min = cfg["min_interval_min"]; changed = True
            if cfg["quiet_hours"] is not None and ch.quiet_hours != cfg["quiet_hours"]: ch.quiet_hours = cfg["quiet_hours"]; changed = True
            rw = True if cfg["rewrite"] is None else bool(cfg["rewrite"])
            ap = bool(cfg["autopilot"])
            if ch.autopilot != ap: ch.autopilot = ap; changed = True
            if cfg["autopilot_min_score"] and ch.autopilot_min_score != cfg["autopilot_min_score"]: ch.autopilot_min_score = cfg["autopilot_min_score"]; changed = True
            ric = True if cfg["review_if_uncertain"] is None else bool(cfg["review_if_uncertain"])
            if ch.review_if_uncertain != ric: ch.review_if_uncertain = ric; changed = True
            dc = bool(cfg["double_check"])
            if ch.double_check != dc: ch.double_check = dc; changed = True
            if ch.rewrite_enabled != rw: ch.rewrite_enabled = rw; changed = True
            if ch.style_profile_id != style_id: ch.style_profile_id = style_id; changed = True
            if changed: stats["targets"][1] += 1
        await session.flush()
        target_ids = {c.username: c.id for c in (await session.execute(select(TargetChannel))).scalars().all()}



        keep = set()
        existing_s = {s.username: s for s in (await session.execute(select(Source))).scalars().all()}
        for e in sources_cfg:
            tgt = target_ids.get(e["target"])
            src = existing_s.get(e["username"])
            if src is None:
                src = Source(username=e["username"], kind=e["kind"], enabled=e["enabled"], target_channel_id=tgt,
                             poll_interval_sec=e["poll_interval_sec"] or d_interval,
                             fresh_window_min=e["fresh_window_min"] or d_window,
                             fallback_count=e["fallback_count"] if e["fallback_count"] is not None else d_fb,
                             fallback_max_age_hours=e["fallback_max_age_hours"] if e["fallback_max_age_hours"] is not None else d_fb_h,
                             relevance=e["relevance"], filters=e["filters"])
                session.add(src); stats["sources"][0] += 1
                await session.flush(); keep.add(src.id)
            else:
                keep.add(src.id); changed = False
                if src.kind != e["kind"]: src.kind = e["kind"]; changed = True
                if src.enabled != e["enabled"]: src.enabled = e["enabled"]; changed = True
                if src.target_channel_id != tgt: src.target_channel_id = tgt; changed = True
                if e["poll_interval_sec"] and src.poll_interval_sec != e["poll_interval_sec"]: src.poll_interval_sec = e["poll_interval_sec"]; changed = True
                if e["fresh_window_min"] and src.fresh_window_min != e["fresh_window_min"]: src.fresh_window_min = e["fresh_window_min"]; changed = True
                if e["fallback_count"] is not None and src.fallback_count != e["fallback_count"]: src.fallback_count = e["fallback_count"]; changed = True
                if e["fallback_max_age_hours"] is not None and src.fallback_max_age_hours != e["fallback_max_age_hours"]: src.fallback_max_age_hours = e["fallback_max_age_hours"]; changed = True
                if src.relevance != e["relevance"]: src.relevance = e["relevance"]; changed = True
                if src.filters != e["filters"]: src.filters = e["filters"]; changed = True
                if changed: stats["sources"][1] += 1
        for src in existing_s.values():
            if src.id not in keep and src.enabled:
                src.enabled = False; stats["disabled"] += 1
        await session.commit()
    return stats


async def sync_sources(path=DEFAULT_PATH):
    p = Path(path)
    if not p.exists():
        return None
    return await apply_parsed(parse_sources_text(p.read_text(encoding="utf-8")))


async def apply_sources_text(text: str):
    return await apply_parsed(parse_sources_text(text))
