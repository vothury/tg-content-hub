from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from app.db.models import Article, EditorialWebSource, Headline, Topic
from app.db.session import session_scope
from app.web.auth import get_csrf_token, require_auth
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/editorial")
async def editorial_page(request: Request):
    async with session_scope() as session:
        topics = (await session.execute(
            select(Topic).order_by(Topic.id.desc()).limit(50))).scalars().all()
        articles = (await session.execute(
            select(Article).order_by(Article.id.desc()).limit(100))).scalars().all()
        web_sources = (await session.execute(
            select(EditorialWebSource).order_by(EditorialWebSource.id))).scalars().all()
        head_count = (await session.execute(
            select(func.count()).select_from(Headline))).scalar() or 0
        headlines = (await session.execute(
            select(Headline).order_by(Headline.id.desc()).limit(100))).scalars().all()
    art_map = {a.topic_id: a for a in articles}
    return templates.TemplateResponse(request, "editorial.html", {
        "active": "editorial",
        "csrf_token": get_csrf_token(request),
        "topics": topics,
        "articles": art_map,
        "web_sources": web_sources,
        "head_count": head_count,
        "headlines": headlines,
    })