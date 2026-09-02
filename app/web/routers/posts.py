from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.db.enums import PostStatus
from app.db.models import Post, Source, TargetChannel
from app.db.session import session_scope
from app.web.auth import get_csrf_token, require_auth
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/posts")
async def posts_list(request: Request, status: str = "", channel: int = 0, q: str = ""):
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
            query.order_by(Post.id.desc()).limit(100)
        )).scalars().all()


        sources = {s.id: s.username for s in (
            await session.execute(select(Source))
        ).scalars().all()}
        channels = (await session.execute(
            select(TargetChannel).order_by(TargetChannel.id)
        )).scalars().all()
        ch_map = {c.id: c.username for c in channels}
    rows = [
        {
            "id": p.id,
            "status": p.status.value,
            "source": sources.get(p.source_id, "?"),
            "channel": ch_map.get(p.target_channel_id, "—"),
            "score": p.score,
            "text": (p.original_text or "")[:80],
        }
        for p in posts
    ]
    return templates.TemplateResponse(request, "posts.html", {
        "active": "posts",
        "csrf_token": get_csrf_token(request),
        "rows": rows,
        "channels": channels,
        "statuses": [s.value for s in PostStatus],
        "f_status": status, "f_channel": channel, "f_q": q,
    })