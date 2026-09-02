from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.config import settings
from app.db.models import AppSetting
from app.db.session import session_scope
from app.services.settings import Keys, get_setting
from app.web.auth import csrf_protect, get_csrf_token, require_auth
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_auth)])

SENSITIVE = {
    "bot_token", "openrouter_api_key", "admin_password", "secret_key",
    "telegram_api_hash", "database_url",
}


def _k(name, fallback):
    return getattr(Keys, name, fallback)


EDITABLE = [
    {"key": _k("CLASSIFY_MODEL", "llm.classify_model"), "label": "Модель классификации", "attr": "llm_classify_model", "type": "text"},
    {"key": _k("REWRITE_MODEL", "llm.rewrite_model"), "label": "Модель рерайта", "attr": "llm_rewrite_model", "type": "text"},
    {"key": _k("REVISION_MODEL", "llm.revision_model"), "label": "Модель правки", "attr": "llm_revision_model", "type": "text"},
    {"key": _k("CLASSIFY_PROVIDERS", "llm.classify_providers"), "label": "Провайдеры классификации (JSON)", "attr": "llm_classify_providers", "type": "text"},
    {"key": _k("MAX_LLM_BUDGET_USD_PER_DAY", "limits.max_llm_budget_usd_per_day"), "label": "Бюджет LLM, $/день", "attr": "max_llm_budget_usd_per_day", "type": "number"},
    {"key": _k("MAX_MEDIA_DOWNLOAD_MB", "reader.max_media_download_mb"), "label": "Макс. размер медиа, МБ", "attr": "max_media_download_mb", "type": "number"},
    {"key": _k("PREFILTER_BLACKLIST_WORDS", "prefilter.blacklist_words"), "label": "Блэклист слов (через запятую)", "attr": "prefilter_blacklist_words", "type": "list"},
    {"key": _k("READER_DEFAULT_SOURCE_INTERVAL_SEC", "reader.default_source_interval_sec"), "label": "Интервал опроса источника, сек", "attr": "reader_default_source_interval_sec", "type": "number"},
]


@router.get("/settings")
async def settings_page(request: Request, msg: str = ""):
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
        editable = []
        for e in EDITABLE:
            editable.append({
                "key": e["key"], "label": e["label"], "type": e["type"],
                "current": await get_setting(session, e["key"]),
                "default": getattr(settings, e["attr"], ""),
            })
    return templates.TemplateResponse(request, "settings.html", {
        "active": "settings",
        "csrf_token": get_csrf_token(request),
        "msg": msg,
        "rows": rows,
        "overrides": overrides,
        "editable": editable,
    })


@router.post("/settings/save", dependencies=[Depends(csrf_protect)])
async def settings_save(request: Request, key: str = Form(...), value: str = Form(...), vtype: str = Form("text")):
    val = value.strip()
    if vtype == "number":
        try:
            val = int(val) if val.lstrip("-").isdigit() else float(val)
        except ValueError:
            return RedirectResponse("/settings?msg=ошибка+значения", status_code=303)
    elif vtype == "list":
        val = [w.strip() for w in val.replace("\n", ",").split(",") if w.strip()]
    async with session_scope() as session:
        row = (await session.execute(
            select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
        if row is None:
            session.add(AppSetting(key=key, value=val))
        else:
            row.value = val
        await session.commit()
    return RedirectResponse(f"/settings?msg={quote('сохранено')}", status_code=303)


@router.post("/settings/reset", dependencies=[Depends(csrf_protect)])
async def settings_reset(request: Request, key: str = Form(...)):
    async with session_scope() as session:
        row = (await session.execute(
            select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
        if row is not None:
            await session.delete(row)
        await session.commit()
    return RedirectResponse(f"/settings?msg={quote('сброшено к дефолту')}", status_code=303)