"""Страница редакции: табы Ревью/Темы/Поток/Конфиг + действия по статьям.

Таб «Ревью» — рабочее место владельца: карточки статей с досье (рубрика,
новизна, опора гипотезы, материалы, отчёт редактора) и действиями
одобрить/отклонить; досье читается из articles.review, посчитанного при написании.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from app.db.enums import (ArticleStatus, EventActor, PostStatus, PublishJobState,
                          PublishMode, TopicStatus)
from app.db.models import (Article, EditorialWebSource, Headline, Material, Post,
                           PostEvent, PublishJob, Source, TargetChannel, Topic)
from app.db.session import session_scope
from app.services.publishing import create_publish_job
from app.services.settings import Keys, get_setting
from app.services.times import owner_now
from app.web.auth import csrf_protect, get_csrf_token, require_auth
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_auth)])

_SYNTH_MSG_BASE = 9_000_000_000
_OWNER = getattr(EventActor, "OWNER", EventActor.SYSTEM)


def _j(v):
    return ", ".join(str(x) for x in v) if isinstance(v, list) else v


async def _editorial_cfg() -> dict:
    """Сводка применённой конфигурации редакции для таба «Конфиг» (без кэша)."""
    async with session_scope() as session:
        async def s(key, default="—"):
            v = await get_setting(session, key)
            return default if v is None else v
        ch = (await session.execute(
            select(TargetChannel).where(TargetChannel.editorial.is_(True))
            .order_by(TargetChannel.id).limit(1))).scalar_one_or_none()
        cfg = {
            "enabled": int(await s(Keys.EDITORIAL_ENABLED, 0)),
            "cycle_times": _j(await s(Keys.EDITORIAL_CYCLE_TIMES)),
            "publish_windows": _j(await s(Keys.EDITORIAL_PUBLISH_WINDOWS)),
            "prepared_hours": await s(Keys.EDITORIAL_PREPARED_HOURS),
            "published_hours": await s(Keys.EDITORIAL_PUBLISHED_HOURS),
            "writer_mode": await s(Keys.EDITORIAL_WRITER_MODE),
            "writer_model": _j(await s(Keys.EDITORIAL_WRITER_MODEL, None))
                            or _j(await s(Keys.REVISION_MODEL)),
            "writer_hypo": _j(await s(getattr(
                Keys, "EDITORIAL_WRITER_MODEL_HYPOTHESIS",
                "editorial.writer_model_hypothesis"), None)) or "= писателю",
            "chief_model": _j(await s(Keys.EDITORIAL_CHIEF_MODEL)),
            "journalist_model": _j(await s(Keys.EDITORIAL_JOURNALIST_MODEL)),
            "browse_model": _j(await s(Keys.EDITORIAL_BROWSE_MODEL)),
            "browse_enabled": int(await s(Keys.EDITORIAL_BROWSE_ENABLED, 0)),
            "auto_publish": int(await s(Keys.EDITORIAL_AUTO_PUBLISH, 0)),
            "max_chars": await s(Keys.EDITORIAL_POST_MAX_CHARS),
            "rewrite_max": await s(Keys.EDITORIAL_REWRITE_MAX_PER_DAY),
            "budget": await s(Keys.EDITORIAL_BUDGET_USD_PER_DAY),
            "topics_per_cycle": await s(Keys.EDITORIAL_TOPICS_PER_CYCLE, 2),
            "retention": await s(Keys.EDITORIAL_HEADLINE_RETENTION_DAYS, 30),
            "fallback": _j(await s(Keys.LLM_FALLBACK_MODELS, None)) or "—",
            "channel_username": ch.username if ch else None,
            "channel_paused": bool(ch.paused) if ch else False,
            "channel_autopilot": bool(ch.autopilot) if ch else False,
            "channel_daily_limit": ch.daily_limit if ch else "—",
            "channel_windows": _j(ch.publish_windows) if ch and ch.publish_windows
                               else "круглые сутки",
        }
    from app.redis_client import get_redis
    spent = float(await get_redis().get(
        f"guard:editorial_cost:{owner_now().date().isoformat()}") or 0)
    cfg["spent"] = round(spent, 4)
    return cfg


async def _editorial_source_id(session) -> int | None:
    return (await session.execute(
        select(Source.id).where(Source.username == "virtual_editorial")
    )).scalar_one_or_none()


async def _editorial_channel_id(session) -> int | None:
    return (await session.execute(
        select(TargetChannel.id).where(TargetChannel.editorial.is_(True))
        .order_by(TargetChannel.id).limit(1))).scalar_one_or_none()


async def _counters() -> dict:
    day_start = owner_now().replace(hour=0, minute=0, second=0, microsecond=0)
    async with session_scope() as session:
        src = await _editorial_source_id(session)
        ch = await _editorial_channel_id(session)
        awaiting = (await session.scalar(
            select(func.count()).select_from(Post).where(
                Post.source_id == src, Post.status == PostStatus.AWAITING_REVIEW)
        )) if src else 0
        scheduled = (await session.scalar(
            select(func.count()).select_from(PublishJob).where(
                PublishJob.target_channel_id == ch,
                PublishJob.state.in_([PublishJobState.QUEUED, PublishJobState.SCHEDULED])
            ))) if ch else 0
        published = (await session.scalar(
            select(func.count()).select_from(PublishJob).where(
                PublishJob.target_channel_id == ch, PublishJob.state == PublishJobState.DONE,
                PublishJob.published_at >= day_start.astimezone(timezone.utc))
        )) if ch else 0
        dropped = await session.scalar(
            select(func.count()).select_from(Topic).where(
                Topic.status == TopicStatus.DROPPED,
                Topic.updated_at >= day_start.astimezone(timezone.utc)))
    return {"awaiting": awaiting or 0, "scheduled": scheduled or 0,
            "published_today": published or 0, "dropped_today": dropped or 0}


async def _review_cards() -> list:
    async with session_scope() as session:
        src = await _editorial_source_id(session)
        posts = (await session.execute(
            select(Post).where(Post.source_id == src,
                               Post.status == PostStatus.AWAITING_REVIEW)
            .order_by(Post.id.desc()).limit(20))).scalars().all() if src else []
        cards = []
        for p in posts:
            art_id = (p.source_message_id or 0) - _SYNTH_MSG_BASE
            art = await session.get(Article, art_id) if art_id > 0 else None
            topic = await session.get(Topic, art.topic_id) if art else None
            mats = (await session.execute(
                select(Material.title, Material.url)
                .where(Material.topic_id == topic.id).order_by(Material.id)
            )).all() if topic else []
            text = p.draft_text or p.original_text or ""
            title_line, _, body = text.partition("\n\n")
            cards.append({
                "post_id": p.id,
                "created_at": p.created_at,
                "status": p.status.value,
                "kind": topic.kind.value if topic else "—",
                "title": title_line or "(без заголовка)",
                "lead": (body or "")[:220],
                "review": art.review if art else None,
                "materials": [{"title": t, "url": u} for t, u in mats],
            })
    return cards


@router.get("/editorial")
async def editorial_page(request: Request, tab: str = "review"):
    if tab not in ("review", "topics", "flow", "config"):
        tab = "review"
    cfg = await _editorial_cfg()
    counters = await _counters()
    cards = await _review_cards() if tab == "review" else []
    topics = headlines = web_sources = []
    async with session_scope() as session:
        if tab == "topics":
            topics = (await session.execute(
                select(Topic).order_by(Topic.id.desc()).limit(100))).scalars().all()
        if tab == "flow":
            headlines = (await session.execute(
                select(Headline).order_by(Headline.id.desc()).limit(100))
            ).scalars().all()
            web_sources = (await session.execute(
                select(EditorialWebSource).order_by(EditorialWebSource.id))
            ).scalars().all()
    head_count = 0
    if tab == "flow":
        async with session_scope() as session:
            head_count = (await session.execute(
                select(func.count()).select_from(Headline))).scalar() or 0
    return templates.TemplateResponse(request, "editorial.html", {
        "active": "editorial",
        "csrf_token": get_csrf_token(request),
        "tab": tab,
        "cfg": cfg,
        "counters": counters,
        "cards": cards,
        "topics": topics,
        "headlines": headlines,
        "web_sources": web_sources,
        "head_count": head_count,
    })


@router.post("/editorial/approve/{post_id}", dependencies=[Depends(csrf_protect)])
async def editorial_approve(post_id: int):
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None or post.status is not PostStatus.AWAITING_REVIEW:
            return RedirectResponse("/editorial", status_code=303)
        post.status = PostStatus.APPROVED
        post.approved_at = datetime.now(timezone.utc)
        session.add(PostEvent(
            post_id=post_id, actor=_OWNER, action="approved",
            from_status=PostStatus.AWAITING_REVIEW.value,
            to_status=PostStatus.APPROVED.value,
            details={"via": "editorial_page"}))
        await session.commit()
    mode = getattr(PublishMode, "SCHEDULED", PublishMode.NOW)
    await create_publish_job(post_id, mode)
    return RedirectResponse("/editorial", status_code=303)


@router.post("/editorial/reject/{post_id}", dependencies=[Depends(csrf_protect)])
async def editorial_reject(post_id: int, reason: str = Form("")):
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None or post.status is not PostStatus.AWAITING_REVIEW:
            return RedirectResponse("/editorial", status_code=303)
        post.status = PostStatus.UNSUITABLE
        post.reject_reason = reason.strip() or "отклонено владельцем на ревью"
        art_id = (post.source_message_id or 0) - _SYNTH_MSG_BASE
        art = await session.get(Article, art_id) if art_id > 0 else None
        if art is not None:
            topic = await session.get(Topic, art.topic_id)
            if topic is not None:
                topic.status = TopicStatus.DROPPED
                topic.verdict = "refuted"
                topic.verdict_note = f"владелец: {post.reject_reason}"
        session.add(PostEvent(
            post_id=post_id, actor=_OWNER, action="rejected",
            from_status=PostStatus.AWAITING_REVIEW.value,
            to_status=PostStatus.UNSUITABLE.value,
            details={"reason": post.reject_reason, "via": "editorial_page"}))
        await session.commit()
    return RedirectResponse("/editorial", status_code=303)