from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.db.models import Source, StyleProfile, TargetChannel
from app.db.session import session_scope
from app.services import config_yaml
from app.services.sources_sync import SourcesFileError
from app.web.auth import csrf_protect, get_csrf_token, require_auth
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/content")
async def content_page(request: Request, msg: str = "", err: str = ""):
    meta = await config_yaml.get_meta()
    async with session_scope() as session:
        sources = (await session.execute(select(Source).order_by(Source.id))).scalars().all()
        channels = (await session.execute(select(TargetChannel).order_by(TargetChannel.id))).scalars().all()
        styles = (await session.execute(select(StyleProfile).order_by(StyleProfile.id))).scalars().all()
        ch_map = {c.id: c.username for c in channels}
    return templates.TemplateResponse(request, "content.html", {
        "active": "content", "csrf_token": get_csrf_token(request),
        "msg": msg, "err": err, "meta": meta,
        "editor_text": meta["draft"] or meta["file_text"],
        "sources": sources, "channels": channels, "styles": styles, "ch_map": ch_map,
    })


@router.post("/content/apply", dependencies=[Depends(csrf_protect)])
async def content_apply(request: Request, text: str = Form(...)):
    try:
        stats = await config_yaml.web_apply(text)
    except SourcesFileError as exc:
        return RedirectResponse(f"/content?err={quote(str(exc))}", status_code=303)
    msg = f"применено: стилей +{stats['styles'][0]}/~{stats['styles'][1]}, каналов +{stats['targets'][0]}/~{stats['targets'][1]}, источников +{stats['sources'][0]}/~{stats['sources'][1]}, отключено {stats['disabled']}"
    return RedirectResponse(f"/content?msg={quote(msg)}", status_code=303)


@router.post("/content/save_draft", dependencies=[Depends(csrf_protect)])
async def content_save_draft(request: Request, text: str = Form(...)):
    await config_yaml.save_draft(text)
    return RedirectResponse("/content?msg=черновик+сохранён", status_code=303)


@router.post("/content/reload", dependencies=[Depends(csrf_protect)])
async def content_reload(request: Request):
    text = config_yaml.read_file_text()
    if text is None:
        return RedirectResponse("/content?err=sources.yaml+не+найден+на+сервере", status_code=303)
    await config_yaml.save_draft(text)
    return RedirectResponse("/content?msg=загружено+с+сервера", status_code=303)