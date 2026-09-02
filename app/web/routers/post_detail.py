from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.db.models import (
    MediaItem, Post, PostDraftVersion, PostEvent, Source, TargetChannel,
)
from app.db.session import session_scope
from app.services import review
from app.web.auth import csrf_protect, get_csrf_token, require_auth
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_auth)])


def _back(post_id: int, res) -> RedirectResponse:
    return RedirectResponse(
        f"/posts/{post_id}?msg={quote(res.message)}", status_code=303)


@router.get("/posts/{post_id}")
async def post_detail(request: Request, post_id: int, msg: str = ""):
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return RedirectResponse("/posts", status_code=303)
        source = await session.get(Source, post.source_id)
        channel = await session.get(TargetChannel, post.target_channel_id) \
            if post.target_channel_id else None
        media = (await session.execute(select(MediaItem)
            .where(MediaItem.post_id == post_id)
            .order_by(MediaItem.position))).scalars().all()
        versions = (await session.execute(select(PostDraftVersion)
            .where(PostDraftVersion.post_id == post_id)
            .order_by(PostDraftVersion.version.desc()))).scalars().all()
        events = (await session.execute(select(PostEvent)
            .where(PostEvent.post_id == post_id)
            .order_by(PostEvent.id.desc()).limit(20))).scalars().all()
        channels = (await session.execute(select(TargetChannel)
            .order_by(TargetChannel.id))).scalars().all()
    return templates.TemplateResponse(request, "post_detail.html", {
        "active": "posts",
        "csrf_token": get_csrf_token(request),
        "msg": msg,
        "p": post,
        "status": post.status.value,
        "source": source.username if source else "?",
        "channel": channel.username if channel else "—",
        "media": media,
        "versions": versions,
        "events": events,
        "channels": channels,
    })


@router.post("/posts/{post_id}/approve", dependencies=[Depends(csrf_protect)])
async def act_approve(request: Request, post_id: int, target_channel_id: int = Form(0)):
    return _back(post_id, await review.approve(post_id, target_channel_id or None))


@router.post("/posts/{post_id}/reject", dependencies=[Depends(csrf_protect)])
async def act_reject(request: Request, post_id: int, reason: str = Form("")):
    return _back(post_id, await review.reject(post_id, reason))


@router.post("/posts/{post_id}/ai", dependencies=[Depends(csrf_protect)])
async def act_ai(request: Request, post_id: int, comment: str = Form(...)):
    return _back(post_id, await review.apply_ai_revision(post_id, comment))


@router.post("/posts/{post_id}/edit", dependencies=[Depends(csrf_protect)])
async def act_edit(request: Request, post_id: int, text: str = Form(...)):
    return _back(post_id, await review.apply_manual_edit(post_id, text))