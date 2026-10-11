from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from app.db.models import Article, EditorialWebSource, Headline, TargetChannel, Topic
from app.db.session import session_scope
from app.redis_client import get_redis
from app.services.settings import Keys, get_setting
from app.services.times import owner_now
from app.web.auth import get_csrf_token, require_auth
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_auth)])


def _j(v):
    return ", ".join(str(x) for x in v) if isinstance(v, list) else v


async def _editorial_cfg() -> dict:
    """Сводка применённой конфигурации редакции для страницы (без кэша)."""
    async with session_scope() as session:
        async def s(key, default="—"):
            v = await get_setting(session, key)
            return default if v is None else v
        ch = (await session.execute(
            select(TargetChannel).where(TargetChannel.editorial.is_(True))
            .order_by(TargetChannel.id).limit(1))).scalar_one_or_none()
        cfg = {
            "enabled": int(await s(Keys.EDITORIAL_ENABLED, 0)),
            "cycle_times": _j(await s(Keys.EDITORIAL_CYCLE_TIMES)),
            "publish_windows": _j(await s(Keys.EDITORIAL_PUBLISH_WINDOWS)),
            "prepared_hours": await s(Keys.EDITORIAL_PREPARED_HOURS),
            "published_hours": await s(Keys.EDITORIAL_PUBLISHED_HOURS),
            "writer_mode": await s(Keys.EDITORIAL_WRITER_MODE),
            "writer_model": _j(await s(Keys.EDITORIAL_WRITER_MODEL, None))
                            or _j(await s(Keys.REVISION_MODEL)),
            "writer_hypo": _j(await s(getattr(
                Keys, "EDITORIAL_WRITER_MODEL_HYPOTHESIS",
                "editorial.writer_model_hypothesis"), None)) or "= писателю",
            "chief_model": _j(await s(Keys.EDITORIAL_CHIEF_MODEL)),
            "journalist_model": _j(await s(Keys.EDITORIAL_JOURNALIST_MODEL)),
            "browse_model": _j(await s(Keys.EDITORIAL_BROWSE_MODEL)),
            "browse_enabled": int(await s(Keys.EDITORIAL_BROWSE_ENABLED, 0)),
            "auto_publish": int(await s(Keys.EDITORIAL_AUTO_PUBLISH, 0)),
            "max_chars": await s(Keys.EDITORIAL_POST_MAX_CHARS),
            "rewrite_max": await s(Keys.EDITORIAL_REWRITE_MAX_PER_DAY),
            "budget": await s(Keys.EDITORIAL_BUDGET_USD_PER_DAY),
            "topics_per_cycle": await s(Keys.EDITORIAL_TOPICS_PER_CYCLE, 2),
            "retention": await s(Keys.EDITORIAL_HEADLINE_RETENTION_DAYS, 30),
            "fallback": _j(await s(Keys.LLM_FALLBACK_MODELS, None)) or "—",
            "channel_username": ch.username if ch else None,
            "channel_paused": bool(ch.paused) if ch else False,
            "channel_autopilot": bool(ch.autopilot) if ch else False,
            "channel_daily_limit": ch.daily_limit if ch else "—",
            "channel_windows": _j(ch.publish_windows) if ch and ch.publish_windows
                               else "круглые сутки",
        }
    spent = float(await get_redis().get(
        f"guard:editorial_cost:{owner_now().date().isoformat()}") or 0)
    cfg["spent"] = round(spent, 4)
    return cfg


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
    cfg = await _editorial_cfg()
    return templates.TemplateResponse(request, "editorial.html", {
        "active": "editorial",
        "csrf_token": get_csrf_token(request),
        "cfg": cfg,
        "topics": topics,
        "articles": art_map,
        "web_sources": web_sources,
        "head_count": head_count,
        "headlines": headlines,
    })