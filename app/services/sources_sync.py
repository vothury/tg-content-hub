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
from app.db.models import EditorialWebSource, Source, StyleProfile, TargetChannel
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

    web = []
    for i, w in enumerate(raw.get("editorial_web") or [], 1):
        url = str(w.get("url") or "").strip()
        if not url:
            raise SourcesFileError(f"editorial_web №{i}: обязательно url")
        web.append({
            "name": str(w.get("name") or "").strip() or url,
            "url": url,
            "feed_url": str(w.get("feed_url") or "").strip() or None,
            "list_selector": str(w.get("list_selector") or "").strip() or None,
            "title_selector": str(w.get("title_selector") or "").strip() or None,
            "link_selector": str(w.get("link_selector") or "").strip() or None,
            "rewrite_source": bool(w.get("rewrite_source", False)),
        })

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
            "dup_recap": bool(t.get("dup_recap", False)),
            "editorial": bool(t.get("editorial", False)),
            "no_review": bool(t.get("no_review", False)),
            "aggregate_mode": str(t.get("aggregate_mode") or "credit").strip(),
            "autopilot_sig_guard": True if t.get("autopilot_sig_guard") is None
                                     else bool(t.get("autopilot_sig_guard")),
            "style": str(t.get("style") or "").strip() or None,
            "autopilot": t.get("autopilot"),
            "autopilot_min_score": t.get("autopilot_min_score"),
            "review_if_uncertain": t.get("review_if_uncertain"),
            "double_check": t.get("double_check"),
            "double_check_online": t.get("double_check_online"),
            "double_check_fact_strictness": t.get("double_check_fact_strictness"),
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
            "title": str(s.get("title") or "").strip() or None,
            "kind": kind,
            "target": _norm_username(s["target"]) if s.get("target") else None,
            "enabled": bool(s.get("enabled", True)),
            "editorial_only": bool(s.get("editorial_only", False)),
            "llm_instructions": str(s.get("llm_instructions") or "").strip() or None,
            "poll_interval_sec": s.get("poll_interval_sec"),
            "fresh_window_min": s.get("fresh_window_min"),
            "fallback_count": s.get("fallback_count"),
            "fallback_max_age_hours": s.get("fallback_max_age_hours"),
            "relevance": _parse_relevance(s.get("relevance"), i),
            "filters": {k: v for k, v in {
                "min_text_len": f.get("min_text_len"),
                "max_text_len": f.get("max_text_len"),
                "blacklist_words": f.get("blacklist_words"),
            }.items() if v is not None},
        })
    return targets, sources, styles, web


def report(targets, sources, styles, web=None) -> str:
    """Краткий отчёт о содержимом + перекрёстные проверки без применения."""
    problems = []
    t_names = {t["username"] for t in targets}
    st_names = {s["name"] for s in styles}
    seen = set()
    for i, s in enumerate(sources, 1):
        key = (s["username"], s["target"])
        if key in seen:
            problems.append(f"sources №{i}: дубль пары username+target {key}")
        seen.add(key)
        if not s["target"]:
            problems.append(f"sources №{i}: не задан target")
        elif s["target"] not in t_names:
            problems.append(f"sources №{i}: target @{s['target']} не описан в targets")
    for t in targets:
        if t["style"] and t["style"] not in st_names:
            problems.append(f"targets @{t['username']}: style '{t['style']}' не описан в styles")
    web = web or []
    urls = [w["url"] for w in web]
    if len(urls) != len(set(urls)):
        problems.append("editorial_web: есть дубли url")
    ed_targets = [t["username"] for t in targets if t["editorial"]]
    for s in sources:
        if s["editorial_only"] and s["username"] not in t_names:
            problems.append(f"sources: editorial_only @{s['username']} не описан в targets (агрегатор должен быть каналом)")
    head = (f"OK: targets={len(targets)} sources={len(sources)} styles={len(styles)} "
            f"editorial_web={len(web)} editorial_channels={ed_targets}")
    return head if not problems else head + "\n" + "\n".join("• " + p for p in problems)


def load_sources_file(path=DEFAULT_PATH):
    p = Path(path)
    if not p.exists():
        return None
    return parse_sources_text(p.read_text(encoding="utf-8"))


async def apply_parsed(parsed) -> dict:
    targets_cfg, sources_cfg, styles_cfg, web_cfg = parsed
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

        stats["editorial_web"] = [0, 0]
        existing_w = {w.url: w for w in (
            await session.execute(select(EditorialWebSource))).scalars().all()}
        keep_w = set()
        for e in web_cfg:
            w = existing_w.get(e["url"])
            if w is None:
                session.add(EditorialWebSource(
                    name=e["name"], url=e["url"], feed_url=e["feed_url"],
                    list_selector=e["list_selector"], title_selector=e["title_selector"],
                    link_selector=e["link_selector"],
                    rewrite_source=e["rewrite_source"], enabled=True))
                stats["editorial_web"][0] += 1
            else:
                changed = False
                if w.name != e["name"]: w.name = e["name"]; changed = True
                if w.feed_url != e["feed_url"]: w.feed_url = e["feed_url"]; changed = True
                if w.list_selector != e["list_selector"]: w.list_selector = e["list_selector"]; changed = True
                if w.title_selector != e["title_selector"]: w.title_selector = e["title_selector"]; changed = True
                if w.link_selector != e["link_selector"]: w.link_selector = e["link_selector"]; changed = True
                if w.rewrite_source != e["rewrite_source"]: w.rewrite_source = e["rewrite_source"]; changed = True
                if not w.enabled: w.enabled = True; changed = True
                if changed: stats["editorial_web"][1] += 1
            keep_w.add(e["url"])
        for w in existing_w.values():
            if w.url not in keep_w and w.enabled:
                w.enabled = False
        await session.flush()

        existing = {c.username: c for c in (await session.execute(select(TargetChannel))).scalars().all()}
        for cfg in targets_cfg:
            style_id = style_ids.get(cfg["style"])
            ch = existing.get(cfg["username"])
            if ch is None:
                session.add(TargetChannel(username=cfg["username"], title=cfg["title"], description=cfg["description"],
                                          daily_limit=cfg["daily_limit"] or 6, min_interval_min=cfg["min_interval_min"] or 60,
                                          quiet_hours=cfg["quiet_hours"],
                                          rewrite_enabled=True if cfg["rewrite"] is None else bool(cfg["rewrite"]),
                                          dup_recap_enabled=bool(cfg["dup_recap"]),
                                          editorial=bool(cfg["editorial"]),
                                          no_review=bool(cfg["no_review"]),
                                          aggregate_mode=cfg["aggregate_mode"],
                                          autopilot_sig_guard=bool(cfg["autopilot_sig_guard"]),
                                          autopilot=bool(cfg["autopilot"]),
                                          autopilot_min_score=cfg["autopilot_min_score"],
                                          review_if_uncertain=True if cfg["review_if_uncertain"] is None else bool(cfg["review_if_uncertain"]),
                                          double_check=bool(cfg["double_check"]),
                                          double_check_online=bool(cfg["double_check_online"]),
                                          double_check_fact_strictness=cfg["double_check_fact_strictness"],
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
            dr = bool(cfg["dup_recap"])
            if ch.dup_recap_enabled != dr: ch.dup_recap_enabled = dr; changed = True
            ed = bool(cfg["editorial"])
            if ch.editorial != ed: ch.editorial = ed; changed = True
            nr = bool(cfg["no_review"])
            if ch.no_review != nr: ch.no_review = nr; changed = True
            am = cfg["aggregate_mode"]
            if ch.aggregate_mode != am: ch.aggregate_mode = am; changed = True
            sg = bool(cfg["autopilot_sig_guard"])
            if ch.autopilot_sig_guard != sg: ch.autopilot_sig_guard = sg; changed = True
            if ch.style_profile_id != style_id: ch.style_profile_id = style_id; changed = True
            if changed: stats["targets"][1] += 1
            dco = bool(cfg["double_check_online"])
            if ch.double_check_online != dco: ch.double_check_online = dco; changed = True
            if cfg["double_check_fact_strictness"] and ch.double_check_fact_strictness != cfg["double_check_fact_strictness"]: ch.double_check_fact_strictness = cfg["double_check_fact_strictness"]; changed = True
        await session.flush()
        target_ids = {c.username: c.id for c in (await session.execute(select(TargetChannel))).scalars().all()}



        keep = set()
        id2uname = {c.id: c.username for c in (
            await session.execute(select(TargetChannel))).scalars().all()}
        existing_s = {
            (s.username, id2uname.get(s.target_channel_id)): s
            for s in (await session.execute(select(Source))).scalars().all()
        }
        for e in sources_cfg:
            tgt = target_ids.get(e["target"])
            src = existing_s.get((e["username"], e["target"]))
            if src is None:
                src = Source(username=e["username"], title=e["title"] or e["username"],
                             kind=e["kind"], enabled=e["enabled"], target_channel_id=tgt,
                             poll_interval_sec=e["poll_interval_sec"] or d_interval,
                             fresh_window_min=e["fresh_window_min"] or d_window,
                             fallback_count=e["fallback_count"] if e["fallback_count"] is not None else d_fb,
                             fallback_max_age_hours=e["fallback_max_age_hours"] if e["fallback_max_age_hours"] is not None else d_fb_h,
                             relevance=e["relevance"], filters=e["filters"],
                             editorial_only=e["editorial_only"],
                             llm_instructions=e["llm_instructions"])
                session.add(src); stats["sources"][0] += 1
                await session.flush(); keep.add(src.id)
            else:
                keep.add(src.id); changed = False
                if src.kind != e["kind"]: src.kind = e["kind"]; changed = True
                if e["title"] and src.title != e["title"]: src.title = e["title"]; changed = True
                if src.enabled != e["enabled"]: src.enabled = e["enabled"]; changed = True
                if src.target_channel_id != tgt: src.target_channel_id = tgt; changed = True
                if e["poll_interval_sec"] and src.poll_interval_sec != e["poll_interval_sec"]: src.poll_interval_sec = e["poll_interval_sec"]; changed = True
                if e["fresh_window_min"] and src.fresh_window_min != e["fresh_window_min"]: src.fresh_window_min = e["fresh_window_min"]; changed = True
                if e["fallback_count"] is not None and src.fallback_count != e["fallback_count"]: src.fallback_count = e["fallback_count"]; changed = True
                if e["fallback_max_age_hours"] is not None and src.fallback_max_age_hours != e["fallback_max_age_hours"]: src.fallback_max_age_hours = e["fallback_max_age_hours"]; changed = True
                if src.relevance != e["relevance"]: src.relevance = e["relevance"]; changed = True
                if src.editorial_only != e["editorial_only"]: src.editorial_only = e["editorial_only"]; changed = True
                if src.llm_instructions != e["llm_instructions"]:
                    src.llm_instructions = e["llm_instructions"]; changed = True
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
