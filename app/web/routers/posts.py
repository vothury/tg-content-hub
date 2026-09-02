from datetime import timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.db.enums import PostStatus
from app.db.models import MediaItem, Post, Source, TargetChannel
from app.db.session import session_scope
from app.services.times import owner_now, owner_tz
from app.web.auth import get_csrf_token, require_auth
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_auth)])


async def _query_rows(status: str, channel: int, q: str):
    async with session_scope() as session:
        query = select(Post)
        if status:
            try:
                query = query.where(Post.status == PostStatus(status))
            except ValueError:
                pass
        if channel:
            query = query.where(Post.target_channel_id == channel)
        if q:
            query = query.where(Post.original_text.ilike(f"%{q}%"))
        posts = (await session.execute(
            query.order_by(Post.id.desc()).limit(100))).scalars().all()
        sources = {s.id: s.username for s in (
            await session.execute(select(Source))).scalars().all()}
        channels = (await session.execute(
            select(TargetChannel).order_by(TargetChannel.id))).scalars().all()
        ch_map = {c.id: c.username for c in channels}
        ids = [p.id for p in posts]
        media_map: dict = {}
        if ids:
            for pid, mt in (await session.execute(select(
                MediaItem.post_id, MediaItem.media_type
            ).where(MediaItem.post_id.in_(ids)))).all():
                media_map.setdefault(pid, []).append(mt.value)
    now_local = owner_now()

    def _when(dt):
        if dt is None:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(owner_tz())
        return local.strftime("%H:%M") if local.date() == now_local.date() else local.strftime("%d.%m")

    rows = []
    for p in posts:
        txt = " ".join((p.original_text or "").split())
        rows.append({
            "id": p.id,
            "status": p.status.value,
            "source": sources.get(p.source_id, "?"),
            "channel": ch_map.get(p.target_channel_id, "—"),
            "media": media_map.get(p.id, []),
            "when": _when(p.source_published_at or p.created_at),
            "text": txt[:80] + (".." if len(txt) > 80 else ""),
        })
    return rows, channels


@router.get("/posts")
async def posts_list(request: Request, status: str = "", channel: int = 0, q: str = ""):
    rows, channels = await _query_rows(status, channel, q)
    return templates.TemplateResponse(request, "posts.html", {
        "active": "posts",
        "csrf_token": get_csrf_token(request),
        "rows": rows,
        "channels": channels,
        "statuses": [s.value for s in PostStatus],
        "f_status": status, "f_channel": channel, "f_q": q,
    })


@router.get("/api/posts")
async def api_posts(status: str = "", channel: int = 0, q: str = ""):
    rows, _ = await _query_rows(status, channel, q)
    return JSONResponse({"rows": rows})