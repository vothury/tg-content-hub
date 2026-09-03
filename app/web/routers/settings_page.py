from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.config import settings
from app.db.models import AppSetting, Source, StyleProfile, TargetChannel
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

ATTR_TO_KEY = {
    "classify_model": _k("CLASSIFY_MODEL", "llm.classify_model"),
    "rewrite_model": _k("REWRITE_MODEL", "llm.rewrite_model"),
    "revision_model": _k("REVISION_MODEL", "llm.revision_model"),
    "classify_providers": _k("CLASSIFY_PROVIDERS", "llm.classify_providers"),
    "rewrite_providers": _k("REWRITE_PROVIDERS", "llm.rewrite_providers"),
    "revision_providers": _k("REVISION_PROVIDERS", "llm.revision_providers"),
    "max_llm_budget_usd_per_day": _k("MAX_LLM_BUDGET_USD_PER_DAY", "limits.max_llm_budget_usd_per_day"),
    "max_candidates_per_day": _k("MAX_CANDIDATES_PER_DAY", "limits.max_candidates_per_day"),
    "prefilter_min_text_len": _k("PREFILTER_MIN_TEXT_LEN", "prefilter.min_text_len"),
    "prefilter_blacklist_words": _k("PREFILTER_BLACKLIST_WORDS", "prefilter.blacklist_words"),
    "max_media_download_mb": _k("MAX_MEDIA_DOWNLOAD_MB", "reader.max_media_download_mb"),
    "reader_default_source_interval_sec": _k("READER_DEFAULT_SOURCE_INTERVAL_SEC", "reader.default_source_interval_sec"),
}

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
        sources = (await session.execute(select(Source).order_by(Source.id))).scalars().all()
        channels = (await session.execute(select(TargetChannel).order_by(TargetChannel.id))).scalars().all()
        styles = (await session.execute(select(StyleProfile).order_by(StyleProfile.id))).scalars().all()

    ch_map = {c.id: c.username for c in channels}
    style_names = {s.id: s.name for s in styles}

    ov = {o.key: o.value for o in overrides}
    rows = []
    for key in sorted(data):
        val = data[key]
        rk = ATTR_TO_KEY.get(key)
        if rk is not None and rk in ov:
            val = ov[rk]
        if key in SENSITIVE:
            val = "•••" if val else ""
        rows.append((key, val))

    src_lines = []
    for s in sources:
        interval = s.poll_interval_sec or settings.reader_default_source_interval_sec
        line = (f"@{s.username} — {'вкл' if s.enabled else 'выкл'}; "
                f"→ @{ch_map.get(s.target_channel_id, '—')}; опрос {interval} с")
        if s.relevance is not None:
            line += f"; релевантность {s.relevance}"
        src_lines.append(line)

    ch_lines = []
    for c in channels:
        ch_lines.append(
            f"@{c.username} — лимит {c.daily_limit}/день; интервал {c.min_interval_min} мин; "
            f"рерайт {'вкл' if c.rewrite_enabled else 'выкл'}; стиль {style_names.get(c.style_profile_id, 'default')}")

    cur = {e["key"]: e["current"] for e in editable}
    cand = int(cur.get(_k("MAX_CANDIDATES_PER_DAY"), settings.max_candidates_per_day) or 0)
    mode_lines = [
        f"Опрос источников: {cur.get(_k('READER_DEFAULT_SOURCE_INTERVAL_SEC'), settings.reader_default_source_interval_sec)} с",
        f"Свежесть: окно {settings.reader_fresh_window_min} мин; фолбэк {settings.reader_fallback_count} не старше {settings.reader_fallback_max_age_hours} ч",
        f"Медиа: скачивание до {cur.get(_k('MAX_MEDIA_DOWNLOAD_MB'), settings.max_media_download_mb)} МБ",
        f"Классификация: {cur.get(_k('CLASSIFY_MODEL'), settings.classify_model)}; рерайт: {cur.get(_k('REWRITE_MODEL'), settings.rewrite_model)}",
        f"Бюджет LLM: ${cur.get(_k('MAX_LLM_BUDGET_USD_PER_DAY'), settings.max_llm_budget_usd_per_day)}/день; "
        f"кандидатов в день: {'без лимита' if cand >= 100000 else cand}",
    ]
    summary = [("Источники", src_lines), ("Каналы", ch_lines), ("Режим работы", mode_lines)]

    return templates.TemplateResponse(request, "settings.html", {
        "active": "settings",
        "csrf_token": get_csrf_token(request),
        "msg": msg,
        "rows": rows,
        "overrides": overrides,
        "editable": editable,
        "summary": summary,
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