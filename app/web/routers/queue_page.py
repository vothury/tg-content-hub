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


async def _queue_rows():
    async with session_scope() as session:
        jobs = (await session.execute(
            select(PublishJob).order_by(PublishJob.id.desc()).limit(100)
        )).scalars().all()
        channels = (await session.execute(select(TargetChannel))).scalars().all()
        post_ids = [j.post_id for j in jobs]
        texts = {}
        if post_ids:
            for p in (await session.execute(
                    select(Post).where(Post.id.in_(post_ids)))).scalars().all():
                texts[p.id] = p.original_text or ""
    ch_map = {c.id: c.username for c in channels}
    rows = [
        {
            "id": j.id, "post_id": j.post_id, "state": j.state.value,
            "mode": j.mode.value, "channel": ch_map.get(j.target_channel_id, "—"),
            "scheduled_at": j.scheduled_at, "published_at": j.published_at,
            "attempts": j.attempts, "note": j.defer_reason or j.last_error or "",
            "text": _snip(texts.get(j.post_id, "")),
        }
        for j in jobs
    ]
    sig = "|".join(f"{r['id']}:{r['state']}" for r in rows)
    return rows, sig


@router.get("/queue")
async def queue_page(request: Request):
    rows, sig = await _queue_rows()
    return templates.TemplateResponse(request, "queue.html", {
        "active": "queue", "csrf_token": get_csrf_token(request),
        "rows": rows, "sig": sig,
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
