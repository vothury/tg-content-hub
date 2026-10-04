"""Фаза 3 виртуальной редакции: писатель — материалы темы → вердикт → статья → Post на ревью.

Для тем status=IN_WORK без статьи: собираем материалы (заголовки темы, тексты tg-постов,
web-заголовки) + справку из базы знаний (facts_kb), один вызов писателя: вердикт
write|drop + черновик в JSON. write -> строка articles (status=REVIEW) + post штатного
конвейера (status=AWAITING_REVIEW, ручной источник virtual_editorial, черновик-версия
origin=editorial, канал назначения = target_channels.editorial), тема -> READY,
заголовки -> used. drop -> тема -> DROPPED с причиной в verdict_note.
Публикация — только через штатное ревью (карточка бота); auto_publish в v1 фазы не_used.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.db.enums import (ArticleStatus, DraftOrigin, LLMStage, PostStatus,
                          SourceKind, TopicStatus)
from app.db.models import (Article, Headline, Material, Post, PostDraftVersion,
                           Source, TargetChannel, Topic)
from app.db.session import session_scope
from app.services import facts_kb
from app.services.editorial_journalist import _call_json
from app.services.llm.prompts import WRITER_SYSTEM, WRITER_USER
from app.services.llm.schemas import EditorialArticleResult
from app.services.settings import Keys, get_providers, get_setting
from app.services.times import owner_now
from app.redis_client import get_redis

log = logging.getLogger("editorial_writer")

ARTICLES_PER_CYCLE = 1            # дисциплина редакции: 1 статья за цикл, 1-3 поста в день
_SYNTH_MSG_BASE = 9_000_000_000   # синтетические source_message_id редакционных постов
_SOURCE_USERNAME = "virtual_editorial"


async def _writer_model() -> tuple[str, dict | None]:
    """editorial.writer_model; пусто -> модель ревизии по умолчанию."""
    async with session_scope() as session:
        model = (str(await get_setting(session, Keys.EDITORIAL_WRITER_MODEL) or "").strip()
                 or str(await get_setting(session, Keys.REVISION_MODEL) or "").strip())
        providers = await get_providers(session, Keys.REVISION_PROVIDERS)
    return model, providers


async def _ensure_source(session) -> int:
    row = (await session.execute(
        select(Source.id).where(Source.username == _SOURCE_USERNAME))).scalar_one_or_none()
    if row is not None:
        return row
    src = Source(kind=SourceKind.EXTERNAL, title="Виртуальная редакция",
                 username=_SOURCE_USERNAME, manual=True, enabled=True)
    session.add(src)
    await session.flush()
    return src.id


async def _editorial_target(session):
    return (await session.execute(
        select(TargetChannel.id)
        .where(TargetChannel.editorial.is_(True), TargetChannel.enabled.is_(True))
        .order_by(TargetChannel.id).limit(1))).scalar_one_or_none()


async def _gather(session, topic):
    """Материалы темы: заголовки + тексты tg-постов + справка базы знаний."""
    ids = [int(x) for x in (topic.materials or []) if str(x).strip().isdigit()]
    heads = []
    if ids:
        heads = (await session.execute(
            select(Headline).where(Headline.id.in_(ids)))).scalars().all()
    texts = {}
    post_ids = [h.post_id for h in heads if h.post_id]
    if post_ids:
        rows = (await session.execute(
            select(Post.id, Post.original_text).where(Post.id.in_(post_ids)))).all()
        texts = {pid: (t or "")[:1500] for pid, t in rows}
    blocks = []
    for n, h in enumerate(heads, 1):
        body = texts.get(h.post_id)
        when = h.fetched_at.strftime("%d.%m.%Y") if h.fetched_at else ""
        if h.source_kind == "tg" and body:
            blocks.append(f"{n}. [tg {h.source_name or ''} {when}]\n{h.title}\n{body}")
        else:
            blocks.append(f"{n}. [web {h.source_name or ''} {when}]\n{h.title}"
                          + (f"\nurl: {h.url}" if h.url else ""))
        session.add(Material(topic_id=topic.id, url=h.url, post_id=h.post_id,
                             title=h.title, full_text=body or None))
    listing = "\n\n".join(blocks) or "—"
    kb = facts_kb.format_context(facts_kb.search(f"{topic.theme} {topic.hypothesis or ''}"))
    return heads, listing, kb


async def run_writer_phase() -> int:
    async with session_scope() as session:
        enabled = int(await get_setting(session, Keys.EDITORIAL_ENABLED))
        budget = float(await get_setting(session, Keys.EDITORIAL_BUDGET_USD_PER_DAY))
        max_chars = int(await get_setting(session, Keys.EDITORIAL_POST_MAX_CHARS))
    if not enabled:
        return 0
    day = owner_now().date().isoformat()
    spent = float(await get_redis().get(f"guard:editorial_cost:{day}") or 0)
    if spent >= budget:
        log.warning("writer: бюджет редакции исчерпан (%.2f/%.2f) — статьи не пишем",
                    spent, budget)
        return 0
    async with session_scope() as session:
        topics = (await session.execute(
            select(Topic)
            .where(Topic.status == TopicStatus.IN_WORK,
                   ~Topic.id.in_(select(Article.topic_id)))
            .order_by(Topic.id).limit(ARTICLES_PER_CYCLE * 2))).scalars().all()
    model, providers = await _writer_model()
    written = 0
    for topic in topics[:ARTICLES_PER_CYCLE]:
        async with session_scope() as session:
            t = await session.get(Topic, topic.id)
            heads, listing, kb = await _gather(session, t)
            messages = [
                {"role": "system", "content": WRITER_SYSTEM.format(max_chars=max_chars)},
                {"role": "user", "content": WRITER_USER.format(
                    theme=t.theme, hypothesis=t.hypothesis or "—",
                    kind=t.kind.value, materials=listing, kb=kb)},
            ]
            try:
                res = await _call_json(messages, model, providers, 1400,
                                       EditorialArticleResult,
                                       stage=LLMStage.EDITORIAL_WRITE)
            except Exception as exc:  # noqa: BLE001
                log.warning("writer: тема #%s без статьи (технически): %s", t.id, exc)
                continue
            if res.verdict == "drop":
                t.status = TopicStatus.DROPPED
                t.verdict = "refuted"
                t.verdict_note = res.drop_reason or "писатель: материалов недостаточно"
                await session.commit()
                log.info("writer: тема #%s отклонена: %s", t.id, t.verdict_note)
                continue
            art = Article(topic_id=t.id, draft_text=res.text,
                          status=ArticleStatus.REVIEW)
            session.add(art)
            await session.flush()
            src_id = await _ensure_source(session)
            tgt_id = await _editorial_target(session)
            post = Post(source_id=src_id,
                        source_message_id=_SYNTH_MSG_BASE + art.id,
                        original_text=res.text, draft_text=res.text,
                        status=PostStatus.AWAITING_REVIEW,
                        target_channel_id=tgt_id, autopilot=False)
            session.add(post)
            await session.flush()
            session.add(PostDraftVersion(post_id=post.id, version=1,
                                         text=res.text, origin=DraftOrigin.EDITORIAL))
            for h in heads:
                hh = await session.get(Headline, h.id)
                if hh is not None:
                    hh.status = "used"
            t.status = TopicStatus.READY
            t.verdict = "confirmed"
            await session.commit()
            written += 1
            log.info("writer: статья #%s (тема #%s): %s | post #%s -> AWAITING_REVIEW",
                     art.id, t.id, (res.title or "")[:60], post.id)
    return written