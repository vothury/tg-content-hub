from datetime import time, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.db.enums import PublishJobState
from app.db.models import LLMCall, Post, PublishJob, TargetChannel
from app.db.session import session_scope
from app.web.auth import get_csrf_token, require_auth
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_auth)])


def _snip(text: str) -> str:
    t = " ".join((text or "").split())
    return t[:80] + (".." if len(t) > 80 else "")


def _day_range(date_str: str):
    from datetime import datetime
    from app.services.times import owner_tz
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    tz = owner_tz()
    start = datetime.combine(d, time.min, tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(d, time.max, tzinfo=tz).astimezone(timezone.utc)
    return start, end


async def _queue_rows(page: int = 1, per_page: int = 50,
                      date_from: str = "", date_to: str = ""):
    async with session_scope() as session:
        base = select(PublishJob)
        if date_from:
            try:
                base = base.where(PublishJob.created_at >= _day_range(date_from)[0])
            except ValueError:
                pass
        if date_to:
            try:
                base = base.where(PublishJob.created_at <= _day_range(date_to)[1])
            except ValueError:
                pass
        total = (await session.execute(
            select(func.count()).select_from(base.subquery()))).scalar() or 0
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(max(1, page), pages)
        jobs = (await session.execute(
            base.order_by(PublishJob.id.desc())
            .limit(per_page).offset((page - 1) * per_page))).scalars().all()
        channels = (await session.execute(select(TargetChannel))).scalars().all()
        post_ids = [j.post_id for j in jobs]
        texts = {}
        if post_ids:
            for p in (await session.execute(
                    select(Post).where(Post.id.in_(post_ids)))).scalars().all():
                texts[p.id] = p.original_text or ""
    ch_map = {c.id: c.username for c in channels}
    ch_link = {c.id: (c.username or f"c/{c.telegram_id}") for c in channels}
    rows = [
        {
            "id": j.id, "post_id": j.post_id, "state": j.state.value,
            "mode": j.mode.value, "channel": ch_map.get(j.target_channel_id, "—"),
            "scheduled_at": j.scheduled_at, "published_at": j.published_at,
            "attempts": j.attempts, "note": j.defer_reason or j.last_error or "",
            "text": _snip(texts.get(j.post_id, "")),
            "url": (f"https://t.me/{ch_link[j.target_channel_id]}/{j.published_message_id}"
                    if j.state is PublishJobState.DONE and j.published_message_id
                    and j.target_channel_id in ch_link else ""),
        }
        for j in jobs
    ]
    sig = "|".join(f"{r['id']}:{r['state']}" for r in rows)
    return rows, sig, total, page, pages


@router.get("/queue")
async def queue_page(request: Request, date_from: str = "", date_to: str = "", page: int = 1):
    rows, sig, total, page, pages = await _queue_rows(page, 50, date_from, date_to)
    base_qs = f"date_from={quote(date_from)}&date_to={quote(date_to)}"
    return templates.TemplateResponse(request, "queue.html", {
        "active": "queue", "csrf_token": get_csrf_token(request),
        "rows": rows, "sig": sig,
        "f_date_from": date_from, "f_date_to": date_to,
        "page": page, "pages": pages, "total": total, "base_qs": base_qs,
    })


@router.get("/api/queue")
async def api_queue():
    _, sig = await _queue_rows()
    return JSONResponse({"sig": sig})


@router.get("/stats")
async def stats_page(request: Request):
    async with session_scope() as session:
        by_day = (await session.execute(
            select(func.date(LLMCall.created_at), func.count(),
                   func.coalesce(func.sum(LLMCall.cost_usd), 0))
            .group_by(func.date(LLMCall.created_at))
            .order_by(func.date(LLMCall.created_at).desc()).limit(14)
        )).all()
        by_model = (await session.execute(
            select(LLMCall.model, func.count(), func.coalesce(func.sum(LLMCall.cost_usd), 0))
            .group_by(LLMCall.model)
            .order_by(func.sum(LLMCall.cost_usd).desc())
        )).all()
        pub_by_day = (await session.execute(
            select(func.date(PublishJob.published_at), func.count())
            .where(PublishJob.state == PublishJobState.DONE)
            .group_by(func.date(PublishJob.published_at))
            .order_by(func.date(PublishJob.published_at).desc()).limit(14)
        )).all()
        status_rows = (await session.execute(
            select(Post.status, func.count()).group_by(Post.status)
        )).all()
    return templates.TemplateResponse(request, "stats.html", {
        "active": "stats", "csrf_token": get_csrf_token(request),
        "by_day": by_day, "by_model": by_model,
        "pub_by_day": pub_by_day, "status_rows": status_rows,
    })
