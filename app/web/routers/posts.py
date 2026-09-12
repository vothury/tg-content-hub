from datetime import time, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.db.enums import PostStatus, PublishJobState
from app.db.models import MediaItem, Post, PublishJob, Source, TargetChannel
from app.db.session import session_scope
from app.services.times import owner_now, owner_tz
from app.web.auth import get_csrf_token, require_auth
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_auth)])


_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
           "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def _date_label(dt):
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(owner_tz())
    return f"{local.day} {_MONTHS[local.month - 1]}"


def _pubfmt(dt):
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(owner_tz())
    return local.strftime("%H:%M") if local.date() == owner_now().date() \
        else local.strftime("%d.%m %H:%M")


def _day_range(date_str: str):
    """Границы(owner-local) суток в UTC для фильтра по дате."""
    from datetime import datetime
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    tz = owner_tz()
    start = datetime.combine(d, time.min, tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(d, time.max, tzinfo=tz).astimezone(timezone.utc)
    return start, end


async def _query_rows(status: str, channel: int, q: str,
                      date_from: str = "", date_to: str = "",
                      page: int = 1, per_page: int = 50):
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
        if date_from:
            try:
                query = query.where(Post.created_at >= _day_range(date_from)[0])
            except ValueError:
                pass
        if date_to:
            try:
                query = query.where(Post.created_at <= _day_range(date_to)[1])
            except ValueError:
                pass
        total = (await session.execute(
            select(func.count()).select_from(query.subquery()))).scalar() or 0
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(max(1, page), pages)
        posts = (await session.execute(
            query.order_by(Post.id.desc())
            .limit(per_page).offset((page - 1) * per_page))).scalars().all()
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
        pub_map: dict = {}
        if ids:
            for pid, pat in (await session.execute(select(
                PublishJob.post_id, PublishJob.published_at
            ).where(PublishJob.post_id.in_(ids),
                      PublishJob.state == PublishJobState.DONE))).all():
                if pat and (pid not in pub_map or pat > pub_map[pid]):
                    pub_map[pid] = pat
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
            "pub_time": _pubfmt(pub_map.get(p.id)),
            "date_label": _date_label(p.source_published_at or p.created_at),
        })
    return rows, channels, total, page, pages
    

@router.get("/posts")
async def posts_list(request: Request, status: str = "", channel: int = 0, q: str = "",
                     date_from: str = "", date_to: str = "", page: int = 1):
    rows, channels, total, page, pages = await _query_rows(
        status, channel, q, date_from, date_to, page)
    base_qs = (f"status={quote(status)}&channel={channel}&q={quote(q)}"
               f"&date_from={quote(date_from)}&date_to={quote(date_to)}")
    return templates.TemplateResponse(request, "posts.html", {
        "active": "posts",
        "csrf_token": get_csrf_token(request),
        "rows": rows,
        "channels": channels,
        "statuses": [s.value for s in PostStatus],
        "f_status": status, "f_channel": channel, "f_q": q,
        "f_date_from": date_from, "f_date_to": date_to,
        "page": page, "pages": pages, "total": total, "base_qs": base_qs,
    })


@router.get("/api/posts")
async def api_posts(status: str = "", channel: int = 0, q: str = "",
                    date_from: str = "", date_to: str = "", page: int = 1):
    rows, _, _, _, _ = await _query_rows(status, channel, q, date_from, date_to, page)
    return JSONResponse({"rows": rows})


@router.get("/api/notify")
async def api_notify():
    async with session_scope() as session:
        last = (await session.execute(
            select(func.max(Post.id)).where(Post.status == PostStatus.AWAITING_REVIEW)
        )).scalar()
        n = (await session.execute(
            select(func.count()).select_from(Post).where(Post.status == PostStatus.AWAITING_REVIEW)
        )).scalar()
    return JSONResponse({"last": last or 0, "n": n or 0})