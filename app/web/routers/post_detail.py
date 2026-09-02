from urllib.parse import quote
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import select

from app.db.models import (
    MediaItem, Post, PostDraftVersion, PostEvent, Source, TargetChannel,
)
from app.db.session import session_scope
from app.services import review
from app.web.auth import csrf_protect, get_csrf_token, require_auth
from app.web.templating import templates

from app.config import settings
from app.db.enums import DraftOrigin, EventActor, PostStatus, PublishMode
from app.services.publishing import create_publish_job
from app.services.times import parse_scheduled

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
async def act_approve(
    request: Request,
    post_id: int,
    target_channel_id: int = Form(0),
    mode: str = Form("now"),
    scheduled_at: str = Form(""),
):
    res = await review.approve(post_id, target_channel_id or None)
    if not res.ok:
        return _back(post_id, res)
    try:
        pub_mode = PublishMode(mode)
    except ValueError:
        pub_mode = PublishMode.NOW
    when = None
    if pub_mode is PublishMode.SCHEDULE:
        when = parse_scheduled(scheduled_at.replace("T", " "))
        if when is None:
            pub_mode = PublishMode.QUEUE
    ok, msg = await create_publish_job(post_id, pub_mode, when)
    res.message = f"{res.message} | публикация: {msg}"
    return _back(post_id, res)


@router.post("/posts/{post_id}/reject", dependencies=[Depends(csrf_protect)])
async def act_reject(request: Request, post_id: int, reason: str = Form("")):
    return _back(post_id, await review.reject(post_id, reason))


@router.post("/posts/{post_id}/ai", dependencies=[Depends(csrf_protect)])
async def act_ai(request: Request, post_id: int, comment: str = Form(...)):
    return _back(post_id, await review.apply_ai_revision(post_id, comment))


@router.post("/posts/{post_id}/edit", dependencies=[Depends(csrf_protect)])
async def act_edit(request: Request, post_id: int, text: str = Form(...)):
    return _back(post_id, await review.apply_manual_edit(post_id, text))


@router.post("/posts/{post_id}/retry", dependencies=[Depends(csrf_protect)])
async def act_retry(request: Request, post_id: int):
    return _back(post_id, await review.retry_manual(post_id))


@router.post("/posts/{post_id}/media_ok", dependencies=[Depends(csrf_protect)])
async def act_media_ok(request: Request, post_id: int):
    return _back(post_id, await review.media_approve(post_id))


@router.post("/posts/{post_id}/to_review", dependencies=[Depends(csrf_protect)])
async def act_to_review(request: Request, post_id: int):
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return RedirectResponse("/posts", status_code=303)
        if not post.draft_text:
            post.draft_text = post.original_text or ""
            post.draft_version = 1
            session.add(PostDraftVersion(
                post_id=post_id, version=1, text=post.draft_text,
                origin=DraftOrigin.ORIGINAL))
        post.status = PostStatus.AWAITING_REVIEW
        session.add(PostEvent(
            post_id=post_id, actor=EventActor.OWNER, action="revived_to_review",
            to_status=PostStatus.AWAITING_REVIEW.value))
        await session.commit()
    return RedirectResponse(
        f"/posts/{post_id}?msg={quote('возвращён в ревью')}", status_code=303)


def _media_root() -> Path:
    root = Path(settings.media_dir)
    if not root.is_absolute():
        root = Path("/app") / settings.media_dir
    return root


@router.get("/media/{path:path}")
async def serve_media(path: str):
    root = _media_root().resolve()
    file = (root / path).resolve()
    if not str(file).startswith(str(root)) or not file.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(file)


@router.get("/api/posts/{post_id}")
async def api_post(post_id: int):
    async with session_scope() as session:
        post = await session.get(Post, post_id)
        if post is None:
            return JSONResponse({"status": None})
        return JSONResponse({"status": post.status.value})