"""Страницы «Каналы» и «Источники»: паузы владельца, сводки, эффективная история.

Пауза = колонка paused: sources_sync её не трогает никогда,
enabled остаётся признаком присутствия в топологии sources.yaml.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from app.config import settings
from app.db.enums import PostStatus, PublishJobState
from app.db.models import Post, PublishJob, Source, TargetChannel
from app.db.session import session_scope
from app.web.auth import csrf_protect, get_csrf_token, require_auth
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_auth)])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _pub_counts(session, target_id, since):
    return await session.scalar(
        select(func.count()).select_from(PublishJob).where(
            PublishJob.target_channel_id == target_id,
            PublishJob.state == PublishJobState.DONE,
            PublishJob.published_at >= since))


async def _post_counts(session, source_id, since):
    return await session.scalar(
        select(func.count()).select_from(Post).where(
            Post.source_id == source_id, Post.created_at >= since))


@router.get("/channels")
async def channels_page(request: Request):
    now = _utcnow()
    d1, d7 = now - timedelta(days=1), now - timedelta(days=7)
    async with session_scope() as session:
        chans = (await session.execute(
            select(TargetChannel).order_by(TargetChannel.id))).scalars().all()
        rows = []
        for ch in chans:
            queue = await session.scalar(
                select(func.count()).select_from(Post).where(
                    Post.target_channel_id == ch.id,
                    Post.status.in_([PostStatus.AWAITING_REVIEW, PostStatus.APPROVED,
                                     PostStatus.SCHEDULED])))
            defer = await session.scalar(
                select(PublishJob.defer_reason).where(
                    PublishJob.target_channel_id == ch.id,
                    PublishJob.defer_reason.isnot(None),
                    PublishJob.state.in_([PublishJobState.QUEUED,
                                          PublishJobState.SCHEDULED]))
                .order_by(PublishJob.id.desc()).limit(1))
            src_n = await session.scalar(
                select(func.count()).select_from(Source).where(
                    Source.target_channel_id == ch.id, Source.enabled.is_(True)))
            rows.append({
                "ch": ch,
                "pub24": await _pub_counts(session, ch.id, d1) or 0,
                "pub7": await _pub_counts(session, ch.id, d7) or 0,
                "queue": queue or 0, "defer": defer, "src_n": src_n or 0,
                "cap": ch.history_max_posts if ch.history_max_posts is not None
                       else settings.reader_backfill_limit,
                "cap_own": ch.history_max_posts is not None,
                "win": ch.fresh_window_min if ch.fresh_window_min is not None
                       else settings.reader_fresh_window_min,
                "win_own": ch.fresh_window_min is not None,
            })
    return templates.TemplateResponse(request, "channels.html", {
        "active": "channels",
        "csrf_token": get_csrf_token(request),
        "rows": rows,
    })


@router.get("/sources")
async def sources_page(request: Request):
    now = _utcnow()
    d1, d7 = now - timedelta(days=1), now - timedelta(days=7)
    async with session_scope() as session:
        srcs = (await session.execute(
            select(Source, TargetChannel.username)
            .outerjoin(TargetChannel, TargetChannel.id == Source.target_channel_id)
            .order_by(Source.id))).all()
        rows = []
        for s, tname in srcs:
            ch = (await session.get(TargetChannel, s.target_channel_id)
                  if s.target_channel_id is not None else None)
            cap = (s.backfill_limit if s.backfill_limit is not None
                   else (ch.history_max_posts if ch is not None and ch.history_max_posts is not None
                         else settings.reader_backfill_limit))
            win = (s.fresh_window_min if s.fresh_window_min is not None
                   else (ch.fresh_window_min if ch is not None and ch.fresh_window_min is not None
                         else settings.reader_fresh_window_min))
            rows.append({
                "s": s, "target": tname or "—",
                "p24": await _post_counts(session, s.id, d1) or 0,
                "p7": await _post_counts(session, s.id, d7) or 0,
                "cap": cap, "cap_own": s.backfill_limit is not None,
                "win": win, "win_own": s.fresh_window_min is not None,
                "read_history": ch.read_history if ch is not None else True,
                "ch_paused": bool(ch.paused) if ch is not None else False,
                "last_read_at": s.last_read_at,
            })
    return templates.TemplateResponse(request, "sources.html", {
        "active": "sources",
        "csrf_token": get_csrf_token(request),
        "rows": rows,
    })


@router.post("/channels/{ch_id}/pause", dependencies=[Depends(csrf_protect)])
async def channel_pause(ch_id: int, paused: bool = Form(True)):
    async with session_scope() as session:
        ch = await session.get(TargetChannel, ch_id)
        if ch is not None:
            ch.paused = paused
            await session.commit()
    return RedirectResponse("/channels", status_code=303)


@router.post("/sources/{src_id}/pause", dependencies=[Depends(csrf_protect)])
async def source_pause(src_id: int, paused: bool = Form(True)):
    async with session_scope() as session:
        s = await session.get(Source, src_id)
        if s is not None:
            s.paused = paused
            await session.commit()
    return RedirectResponse("/sources", status_code=303)