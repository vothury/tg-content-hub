"""Фаза 2 виртуальной редакции: главред выбирает темы из собранных заголовков.

Вход: заголовки headlines.status='new', схлопнутые в кластеры близких по тексту
(дубли одного сюжета у разных источников = сигнал важности, маркер [xN]);
профиль канала и недавние темы — в промпте CHIEF.
Выход: строки topics (kind=hypothesis|rewrite, status=scheduled) + привязка
заголовков (topic_id, status='picked'). Гигиена: prune заголовков старше N дней
без темы (настройка editorial.headline_retention_days).
"""
from __future__ import annotations

import difflib
import logging

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update

from app.db.enums import LLMStage, TopicKind, TopicStatus
from app.db.models import Headline, Topic
from app.db.session import session_scope
from app.services.editorial_journalist import _call_json, _journalist_model
from app.services.llm.prompts import CHIEF_SYSTEM, CHIEF_USER
from app.services.llm.schemas import ChiefTopicsResult
from app.services.settings import Keys, get_setting
from app.services.times import owner_now
from app.redis_client import get_redis


log = logging.getLogger("editorial_chief")

_CLUSTER_SIM = 0.86   # порог «это тот же сюжет»
_THEME_SIM = 0.85     # порог «тема уже была недавно»


def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _cluster(rows: list) -> list:
    """Жадная кластеризация заголовков: близкий текст -> один кластер."""
    clusters: list = []
    for hid, kind, name, title in rows:
        for cl in clusters:
            if _sim(cl["title"], title) >= _CLUSTER_SIM:
                cl["ids"].append(hid)
                cl["kinds"].add(kind)
                break
        else:
            clusters.append({"ids": [hid], "title": title, "kinds": {kind}})
    return clusters


async def run_chief_phase() -> int:
    """Один проход главреда за цикл; возвращает число созданных тем."""
    async with session_scope() as session:
        enabled = int(await get_setting(session, Keys.EDITORIAL_ENABLED))
        budget = float(await get_setting(session, Keys.EDITORIAL_BUDGET_USD_PER_DAY))
        max_topics = int(await get_setting(session, Keys.EDITORIAL_TOPICS_PER_CYCLE))
    if not enabled:
        return 0
    from app.common.redis import get_redis   # импорт сверьте с editorial_journalist.py
    day = owner_now().date().isoformat()
    spent = float(await get_redis().get(f"guard:editorial_cost:{day}") or 0)
    if spent >= budget:
        log.warning("chief: бюджет редакции исчерпан (%.2f/%.2f) — темы не предлагаются",
                    spent, budget)
        return 0

    async with session_scope() as session:
        rows = (await session.execute(
            select(Headline.id, Headline.source_kind, Headline.source_name, Headline.title)
            .where(Headline.status == "new")
            .order_by(Headline.id.desc()).limit(150))).all()
        recent = (await session.execute(
            select(Topic.theme).order_by(Topic.id.desc()).limit(15))).scalars().all()
    if not rows:
        log.info("chief: нет заголовков со статусом new — тем не предлагаю")
        return 0

    clusters = _cluster([(r.id, r.source_kind, r.source_name, r.title) for r in rows])[:60]
    listing = "\n".join(
        (f"{n}. [x{len(c['ids'])} {'+'.join(sorted(c['kinds']))}] {c['title']}"
         if len(c["ids"]) > 1 else f"{n}. {c['title']}")
        for n, c in enumerate(clusters, 1))
    messages = [
        {"role": "system", "content": CHIEF_SYSTEM.format(max_topics=max_topics)},
        {"role": "user", "content": CHIEF_USER.format(
            recent="\n".join(f"- {t}" for t in recent) or "—", listing=listing)},
    ]
    model, providers = await _journalist_model()
    try:
        result = await _call_json(messages, model, providers, 900, ChiefTopicsResult,
                                  stage=LLMStage.EDITORIAL_CHIEF)
    except Exception as exc:  # noqa: BLE001
        log.warning("chief: не дал тем (технически): %s", exc)
        return 0

    created = 0
    for t in result.topics[:max_topics]:
        if any(_sim(t["theme"], r) >= _THEME_SIM for r in recent):
            log.info("chief: тема '%s' похожа на недавнюю — пропущена", t["theme"])
            continue
        flat = [hid for i in t["headlines"] if 0 < i <= len(clusters)
                for hid in clusters[i - 1]["ids"]]
        if not flat:
            continue
        async with session_scope() as session:
            topic = Topic(kind=TopicKind(t["kind"]), status=TopicStatus.IN_WORK,
                          theme=t["theme"], hypothesis=t["hypothesis"] or None,
                          materials=flat)   # ids заголовков — задел для фазы 3
            session.add(topic)
            await session.flush()
            await session.execute(
                update(Headline).where(Headline.id.in_(flat))
                .values(topic_id=topic.id, status="picked"))
            await session.commit()
            tid = topic.id
        created += 1
        log.info("chief: тема #%s (%s): %s | заголовков: %d",
                 tid, t["kind"], t["theme"], len(flat))
    if not result.topics:
        log.info("chief: достойных тем нет — цикл без новых тем (это норма)")
    return created


async def prune_headlines(days: int) -> int:
    """Удаляет заголовки старше days дней, не поднятые в темы (ваше решение 3)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with session_scope() as session:
        res = await session.execute(
            delete(Headline).where(Headline.topic_id.is_(None),
                                   Headline.fetched_at < cutoff))
        await session.commit()
        return res.rowcount or 0