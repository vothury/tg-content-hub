from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.config import settings
from app.db.models import AppSetting
from app.db.session import session_scope
from app.web.auth import get_csrf_token, require_auth
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_auth)])

SENSITIVE = {
    "bot_token", "openrouter_api_key", "admin_password", "secret_key",
    "telegram_api_hash", "database_url",
}


@router.get("/settings")
async def settings_page(request: Request):
    data = settings.model_dump()
    rows = []
    for key in sorted(data):
        val = data[key]
        if key in SENSITIVE:
            val = "•••" if val else ""
        rows.append((key, val))
    async with session_scope() as session:
        overrides = (await session.execute(
            select(AppSetting).order_by(AppSetting.key))).scalars().all()
    return templates.TemplateResponse(request, "settings.html", {
        "active": "settings",
        "csrf_token": get_csrf_token(request),
        "rows": rows,
        "overrides": overrides,
    })