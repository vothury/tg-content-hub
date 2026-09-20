import json

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


def _k(name, fallback=None):
    return getattr(Keys, name, fallback)


def _providers_to_str(v) -> str:
    if v is None or v == "":
        return ""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return v
    if isinstance(v, dict):
        return ", ".join(v.get("order") or [])
    if isinstance(v, list):
        return ", ".join(v)
    return str(v)


EDITABLE = [
    {"key": _k("CLASSIFY_MODEL", "llm.classify_model"), "label": "Модель классификации", "attr": "classify_model", "type": "text"},
    {"key": _k("CLASSIFY_PROVIDERS", "llm.classify_providers"), "label": "Провайдеры классификации (через запятую)", "attr": "classify_providers", "type": "providers"},
    {"key": _k("REWRITE_MODEL", "llm.rewrite_model"), "label": "Модель рерайта", "attr": "rewrite_model", "type": "text"},
    {"key": _k("REWRITE_PROVIDERS", "llm.rewrite_providers"), "label": "Провайдеры рерайта (через запятую)", "attr": "rewrite_providers", "type": "providers"},
    {"key": _k("REVISION_MODEL", "llm.revision_model"), "label": "Модель правки", "attr": "revision_model", "type": "text"},
    {"key": _k("REVISION_PROVIDERS", "llm.revision_providers"), "label": "Провайдеры правки (через запятую)", "attr": "revision_providers", "type": "providers"},
    {"key": _k("PREFILTER_MODEL", "llm.prefilter_model"), "label": "Модель очистки (clean)", "attr": "prefilter_model", "type": "text"},
    {"key": _k("PREFILTER_PROVIDERS", "llm.prefilter_providers"), "label": "Провайдеры очистки (через запятую)", "attr": "prefilter_providers", "type": "providers"},
    {"key": _k("DOUBLE_CHECK_MODEL"), "label": "Модель двойной проверки", "attr": "double_check_model", "type": "text"},
    {"key": _k("DOUBLE_CHECK_PROVIDERS"), "label": "Провайдеры двойной проверки (через запятую)", "attr": "double_check_providers", "type": "providers"},
    {"key": _k("DOUBLE_CHECK_ONLINE_MODEL"), "label": "Модель онлайн-фактчекинга", "attr": "double_check_online_model", "type": "text"},
    {"key": _k("DOUBLE_CHECK_ONLINE_PROVIDERS"), "label": "Провайдеры онлайн-фактчекинга (через запятую)", "attr": "double_check_online_providers", "type": "providers"},    
    {"key": _k("MAX_LLM_BUDGET_USD_PER_DAY", "limits.max_llm_budget_usd_per_day"), "label": "Бюджет LLM, $/день", "attr": "max_llm_budget_usd_per_day", "type": "number"},
    {"key": _k("MAX_CANDIDATES_PER_DAY", "limits.max_candidates_per_day"), "label": "Лимит кандидатов в день", "attr": "max_candidates_per_day", "type": "number",
     "hint": "Сколько постов может дойти до стадии CANDIDATE за сутки (предохранитель конвейера)."},
    {"key": _k("LLM_REASONING_MAX_TOKENS"), "label": "LLM: бюджет reasoning", "attr": "llm_reasoning_max_tokens", "type": "number",
     "hint": "Максимум токенов внутренних рассуждений (classify / double-check offline); защищает от зацикливания."},
    {"key": _k("LLM_REASONING_REWRITE"), "label": "LLM: бюджет reasoning (рерайт)", "attr": "llm_reasoning_rewrite", "type": "number",
     "hint": "Рерайт под стиль и правка ИИ: творческая задача, бюджет выше базового."},
    {"key": _k("LLM_REASONING_ONLINE_CHECK"), "label": "LLM: бюджет reasoning (фактчекинг)", "attr": "llm_reasoning_online_check", "type": "number",
     "hint": "Двойная проверка с поиском в интернете: нужен запас на сопоставление фактов."},
    {"key": _k("LLM_REASONING_SMALL"), "label": "LLM: бюджет reasoning (мелкие задачи)", "attr": "llm_reasoning_small", "type": "number",
     "hint": "Чистка подписей, перевод причины, подтверждение дубля: рассуждения почти не нужны."},
    {"key": _k("LLM_DOUBLE_CHECK_MAX_TOKENS"), "label": "LLM: max_tokens двойной проверки", "attr": "llm_double_check_max_tokens", "type": "number",
     "hint": "Токены на финальный ответ двойной проверки ПОСЛЕ рассуждений; итоговый лимит = это значение + бюджет reasoning. Ответ — короткий JSON, 800 достаточно с запасом."},
    {"key": _k("PUBLISH_DUP_RECAP_WINDOW_HOURS"), "label": "Публикация: окно «ранее писали», часов", "attr": "publish_dup_recap_window_hours", "type": "number",
     "hint": "Сколько часов с первой публикации темы считать её «той же темой»; старше — новая тема без блока."},
    {"key": _k("PUBLISH_RESTORE_LINKS"), "label": "Публикация: возвращать съеденные ссылки", "attr": "publish_restore_links", "type": "number",
     "hint": "1 = если в одобренном посте не осталось ни одной ссылки, а в оригинале они были, добавить строку «Подробнее: url»."},
    {"key": _k("EDITORIAL_ENABLED"), "label": "Редакция: включена", "attr": "editorial_enabled", "type": "number",
     "hint": "1 = циклы виртуальной редакции работают; 0 = приостановлены."},
    {"key": _k("EDITORIAL_CYCLE_TIMES"), "label": "Редакция: времена циклов", "attr": "editorial_cycle_times", "type": "text",
     "hint": "Через запятую ЧЧ:ММ; один цикл = журналист → главред → сбор/текст."},
    {"key": _k("EDITORIAL_PUBLISH_WINDOWS"), "label": "Редакция: окна публикации", "attr": "editorial_publish_windows", "type": "text",
     "hint": "Интервалы ЧЧ:ММ-ЧЧ:ММ через запятую; внутри окна выбирается случайная минута."},
    {"key": _k("EDITORIAL_PREPARED_HOURS"), "label": "Редакция: память тем, часов", "attr": "editorial_prepared_hours", "type": "number",
     "hint": "Сколько часов главред помнит темы в работе/готовые (со статусами)."},
    {"key": _k("EDITORIAL_PUBLISHED_HOURS"), "label": "Редакция: память опубликованного, часов", "attr": "editorial_published_hours", "type": "number",
     "hint": "Сколько часов главред помнит заголовки опубликованных материалов."},
    {"key": _k("EDITORIAL_WRITER_MODE"), "label": "Редакция: кто пишет", "attr": "editorial_writer_mode", "type": "text",
     "hint": "journalist (вариант A, дешевле) или model (вариант B, editorial_writer_model)."},
    {"key": _k("EDITORIAL_WRITER_MODEL"), "label": "Редакция: модель автора (B)", "attr": "editorial_writer_model", "type": "text",
     "hint": "Используется только при writer_mode=model."},
    {"key": _k("EDITORIAL_AUTO_PUBLISH"), "label": "Редакция: автопубликация", "attr": "editorial_auto_publish", "type": "number",
     "hint": "0 = статья ждёт одобрения владельца; 1 = публикация по окнам без ревью."},
    {"key": _k("EDITORIAL_POST_MAX_CHARS"), "label": "Редакция: макс. знаков поста", "attr": "editorial_post_max_chars", "type": "number",
     "hint": "Целевой предел 250-400, жёсткий максимум — это значение."},
    {"key": _k("EDITORIAL_REWRITE_MAX_PER_DAY"), "label": "Редакция: рерайтов в день", "attr": "editorial_rewrite_max_per_day", "type": "number",
     "hint": "Ограничение доли рерайтов, чтобы канал оставался аналитикой, а не лентой."},
    {"key": _k("EDITORIAL_BUDGET_USD_PER_DAY"), "label": "Редакция: бюджет $/день", "attr": "editorial_budget_usd_per_day", "type": "number",
     "hint": "Отдельный дневной потолок расходов редакции (поверх общего бюджета)."},
    {"key": _k("EDITORIAL_CHIEF_MODEL"), "label": "Редакция: модель главреда", "attr": "editorial_chief_model", "type": "text",
     "hint": "Пусто = модель ревизии по умолчанию; нужна сильная reasoning-модель."},
    {"key": _k("EDITORIAL_JOURNALIST_MODEL"), "label": "Редакция: модель журналиста", "attr": "editorial_journalist_model", "type": "text",
     "hint": "Пусто = дешёвая prefilter-модель."},
    {"key": _k("EDITORIAL_BROWSE_MODEL"), "label": "Редакция: browse-модель (ходит по url)", "attr": "editorial_browse_model", "type": "text",
     "hint": "Модель с веб-инструментом для чтения страниц; пусто = модель журналиста + ':online'."},
    {"key": _k("EDITORIAL_BROWSE_ENABLED"), "label": "Редакция: browse-модель включена", "attr": "editorial_browse_enabled", "type": "number",
     "hint": "1 = при недоступности страницы вызывать модель с веб-инструментом (фикс ~$0.007 за выход в интернет); 0 = такие источники пропускаются."},
    {"key": _k("AGGREGATE_ACCEPT_DEFAULT"), "label": "Агрегатор: одобрять (дефолт)", "attr": "aggregate_accept_default", "type": "text",
     "hint": "Что считать релевантным для технических каналов, если у канала не задан свой aggregate_accept."},
    {"key": _k("AGGREGATE_REJECT_DEFAULT"), "label": "Агрегатор: отклонять (дефолт)", "attr": "aggregate_reject_default", "type": "text",
     "hint": "Что отклонять для технических каналов, если у канала не задан свой aggregate_reject."},
    {"key": _k("MAX_MEDIA_DOWNLOAD_MB", "reader.max_media_download_mb"), "label": "Макс. размер медиа, МБ", "attr": "max_media_download_mb", "type": "number"},
    {"key": _k("PREFILTER_BLACKLIST_WORDS", "prefilter.blacklist_words"), "label": "Блэклист слов (через запятую)", "attr": "prefilter_blacklist_words", "type": "list"},
    {"key": _k("READER_DEFAULT_SOURCE_INTERVAL_SEC", "reader.default_source_interval_sec"), "label": "Интервал опроса источника, сек", "attr": "reader_default_source_interval_sec", "type": "number"},
    {"key": _k("AUTOPILOT_MIN_SCORE"), "label": "Автопилот: мин. score", "attr": "autopilot_min_score", "type": "number"},
    {"key": _k("DEDUP_WINDOW_DAYS"), "label": "Дедуп: окно, дней", "attr": "dedup_window_days", "type": "number",
     "hint": "За сколько дней искать дубли среди постов того же целевого канала."},
    {"key": _k("DEDUP_CANONICAL_COSINE_MIN"), "label": "Дедуп: порог похожести текста", "attr": "dedup_canonical_cosine_min", "type": "number",
     "hint": "0..1 — косинус по символьным n-граммам канонической формы; выше = строже."},
    {"key": _k("DEDUP_CANONICAL_CONTAINMENT_MIN"), "label": "Дедуп: порог включения", "attr": "dedup_canonical_containment_min", "type": "number",
     "hint": "0..1 — доля n-грамм короткого канона, входящих в длинный; строже косинуса, ловит случай «один список полнее другого»."},
    {"key": _k("DEDUP_FACT_CONTAINMENT_MIN"), "label": "Дедуп: порог фактовых якорей", "attr": "dedup_fact_containment_min", "type": "number",
     "hint": "0..1 — перекрытие множества «якорей» (названия в «…», имена, даты); не зависит от порядка слов."},
    {"key": _k("DEDUP_CANONICAL_MIN_LEN"), "label": "Дедуп: мин. длина канона", "attr": "dedup_canonical_min_len", "type": "number",
     "hint": "Короче этого канон игнорируется (защита от тривиальных постов вроде «🙂»)."},
    {"key": _k("DEDUP_PHASH_MAX_DISTANCE"), "label": "Дедуп: порог pHash", "attr": "dedup_phash_max_distance", "type": "number",
     "hint": "Расстояние Хэмминга 0..64 для «то же изображение»; меньше = строже."},
    {"key": _k("DEDUP_LUMA_MAX_DIFF"), "label": "Дедуп: допуск яркости", "attr": "dedup_luma_max_diff", "type": "number",
     "hint": "0..255 — макс. разница средней яркости изображений для media-матча; чёрное и белое не совпадут."},
    {"key": _k("DEDUP_CONFIRM_MODEL"), "label": "Дедуп: модель сверки текстов", "attr": "dedup_confirm_model", "type": "text",
     "hint": "Модель, которая по полным текстам решает, дубль это или разные факты; пусто = модель очистки (clean)."},
    {"key": _k("DEDUP_CONFIRM_PROVIDERS"), "label": "Дедуп: провайдеры сверки (через запятую)", "attr": "dedup_confirm_providers", "type": "providers",
     "hint": "Пусто = авто-роутинг OpenRouter."},
    {"key": _k("DEDUP_MAX_COMPARE"), "label": "Дедуп: максимум сравнений", "attr": "dedup_max_compare", "type": "number",
     "hint": "Сколько недавних постов канала сравнивать (ограничивает нагрузку)."},
]

ATTR_TO_KEY = {
    "classify_model": _k("CLASSIFY_MODEL"),
    "rewrite_model": _k("REWRITE_MODEL"),
    "revision_model": _k("REVISION_MODEL"),
    "classify_providers": _k("CLASSIFY_PROVIDERS"),
    "rewrite_providers": _k("REWRITE_PROVIDERS"),
    "revision_providers": _k("REVISION_PROVIDERS"),
    "max_llm_budget_usd_per_day": _k("MAX_LLM_BUDGET_USD_PER_DAY"),
    "max_candidates_per_day": _k("MAX_CANDIDATES_PER_DAY"),
    "llm_reasoning_max_tokens": _k("LLM_REASONING_MAX_TOKENS"),
    "llm_reasoning_rewrite": _k("LLM_REASONING_REWRITE"),
    "llm_reasoning_online_check": _k("LLM_REASONING_ONLINE_CHECK"),
    "llm_reasoning_small": _k("LLM_REASONING_SMALL"),    
    "llm_double_check_max_tokens": _k("LLM_DOUBLE_CHECK_MAX_TOKENS"),
    "publish_dup_recap_window_hours": _k("PUBLISH_DUP_RECAP_WINDOW_HOURS"),
    "publish_restore_links": _k("PUBLISH_RESTORE_LINKS"),
    "editorial_enabled": _k("EDITORIAL_ENABLED"),
    "editorial_cycle_times": _k("EDITORIAL_CYCLE_TIMES"),
    "editorial_publish_windows": _k("EDITORIAL_PUBLISH_WINDOWS"),
    "editorial_prepared_hours": _k("EDITORIAL_PREPARED_HOURS"),
    "editorial_published_hours": _k("EDITORIAL_PUBLISHED_HOURS"),
    "editorial_writer_mode": _k("EDITORIAL_WRITER_MODE"),
    "editorial_writer_model": _k("EDITORIAL_WRITER_MODEL"),
    "editorial_auto_publish": _k("EDITORIAL_AUTO_PUBLISH"),
    "editorial_post_max_chars": _k("EDITORIAL_POST_MAX_CHARS"),
    "editorial_rewrite_max_per_day": _k("EDITORIAL_REWRITE_MAX_PER_DAY"),
    "editorial_budget_usd_per_day": _k("EDITORIAL_BUDGET_USD_PER_DAY"),
    "editorial_chief_model": _k("EDITORIAL_CHIEF_MODEL"),
    "editorial_journalist_model": _k("EDITORIAL_JOURNALIST_MODEL"),
    "editorial_browse_model": _k("EDITORIAL_BROWSE_MODEL"),
    "editorial_browse_enabled": _k("EDITORIAL_BROWSE_ENABLED"),
    "aggregate_accept_default": _k("AGGREGATE_ACCEPT_DEFAULT"),
    "aggregate_reject_default": _k("AGGREGATE_REJECT_DEFAULT"),
    "prefilter_min_text_len": _k("PREFILTER_MIN_TEXT_LEN"),
    "prefilter_blacklist_words": _k("PREFILTER_BLACKLIST_WORDS"),
    "max_media_download_mb": _k("MAX_MEDIA_DOWNLOAD_MB"),
    "reader_default_source_interval_sec": _k("READER_DEFAULT_SOURCE_INTERVAL_SEC"),
    "autopilot_min_score": _k("AUTOPILOT_MIN_SCORE"),
    "double_check_model": _k("DOUBLE_CHECK_MODEL"),
    "prefilter_model": _k("PREFILTER_MODEL"),
    "prefilter_providers": _k("PREFILTER_PROVIDERS"),
    "double_check_providers": _k("DOUBLE_CHECK_PROVIDERS"),
    "double_check_online_model": _k("DOUBLE_CHECK_ONLINE_MODEL"),
    "double_check_online_providers": _k("DOUBLE_CHECK_ONLINE_PROVIDERS"),
    "dedup_window_days": _k("DEDUP_WINDOW_DAYS"),
    "dedup_phash_max_distance": _k("DEDUP_PHASH_MAX_DISTANCE"),
    "dedup_luma_max_diff": _k("DEDUP_LUMA_MAX_DIFF"),
    "dedup_confirm_model": _k("DEDUP_CONFIRM_MODEL"),
    "dedup_confirm_providers": _k("DEDUP_CONFIRM_PROVIDERS"),
    "dedup_canonical_containment_min": _k("DEDUP_CANONICAL_CONTAINMENT_MIN"),
    "dedup_fact_containment_min": _k("DEDUP_FACT_CONTAINMENT_MIN"),
    "dedup_canonical_min_len": _k("DEDUP_CANONICAL_MIN_LEN"),
    "dedup_canonical_cosine_min": _k("DEDUP_CANONICAL_COSINE_MIN"),
    "dedup_max_compare": _k("DEDUP_MAX_COMPARE"),
}

GROUPS = [
    ("Виртуальная редакция", ("editorial_",), ()),
    ("Модели и провайдеры", (), ("_model", "_providers")),
    ("LLM: лимиты рассуждений и ответа", ("llm_reasoning_", "llm_double_check_"), ()),
    ("Дедупликация", ("dedup_",), ()),
    ("Публикация, автопилот, recap", ("autopilot_", "publish_"), ()),
    ("Префильтр и медиа", ("prefilter_", "max_media_"), ()),
    ("Лимиты конвейера и reader", ("max_llm_", "max_candidates_", "reader_"), ()),
]



@router.get("/settings")
async def settings_page(request: Request, msg: str = ""):
    data = settings.model_dump()
    async with session_scope() as session:
        overrides = (await session.execute(
            select(AppSetting).order_by(AppSetting.key))).scalars().all()
        editable = []
        for e in EDITABLE:
            current = await get_setting(session, e["key"])
            default = getattr(settings, e["attr"], "")
            if e["type"] == "providers":
                current = _providers_to_str(current)
                default = _providers_to_str(default)
            editable.append({
                "key": e["key"], "label": e["label"], "type": e["type"],
                "attr": e["attr"], "hint": e.get("hint", ""),
                "current": current, "default": default,
            })
        sources = (await session.execute(select(Source).order_by(Source.id))).scalars().all()
        channels = (await session.execute(select(TargetChannel).order_by(TargetChannel.id))).scalars().all()
        styles = (await session.execute(select(StyleProfile).order_by(StyleProfile.id))).scalars().all()

    groups = []
    for title, starts, ends in GROUPS:
        cards = [e for e in editable
                 if e["attr"].startswith(starts) or e["attr"].endswith(ends)]
        if cards:
            groups.append((title, cards))
    placed = {e["attr"] for _, cards in groups for e in cards}
    rest = [e for e in editable if e["attr"] not in placed]
    if rest:
        groups.append(("Прочее", rest))


    ch_map = {c.id: c.username for c in channels}
    style_names = {s.id: s.name for s in styles}
    cur = {e["key"]: e["current"] for e in editable}
    def_interval = cur.get(_k("READER_DEFAULT_SOURCE_INTERVAL_SEC")) or settings.reader_default_source_interval_sec
    def_media = cur.get(_k("MAX_MEDIA_DOWNLOAD_MB")) or settings.max_media_download_mb

    src_lines = []
    for s in sources:
        interval = s.poll_interval_sec or def_interval
        line = (f"@{s.username} — {'вкл' if s.enabled else 'выкл'}; "
                f"→ @{ch_map.get(s.target_channel_id, '—')}; опрос {interval} с")
        if s.relevance is not None:
            line += f"; релевантность {s.relevance}"
        if s.llm_instructions:
            line += "; инструкции модели: да"
        src_lines.append(line)

    ch_lines = [
        f"@{c.username} — лимит {c.daily_limit}/день; интервал {c.min_interval_min} мин; "
        f"рерайт {'вкл' if c.rewrite_enabled else 'выкл'}; стиль {style_names.get(c.style_profile_id, 'default')}"
        for c in channels
    ]

    cand = int(cur.get(_k("MAX_CANDIDATES_PER_DAY")) or settings.max_candidates_per_day)
    mode_lines = [
        f"Опрос источников: {def_interval} с",
        f"Свежесть: окно {settings.reader_fresh_window_min} мин; фолбэк {settings.reader_fallback_count} не старше {settings.reader_fallback_max_age_hours} ч",
        f"Медиа: скачивание до {def_media} МБ",
        f"Классификация: {cur.get(_k('CLASSIFY_MODEL')) or settings.classify_model}; рерайт: {cur.get(_k('REWRITE_MODEL')) or settings.rewrite_model}",
        f"Двойная проверка: {cur.get(_k('DOUBLE_CHECK_MODEL')) or settings.effective_revision_model}",
        f"Бюджет LLM: ${cur.get(_k('MAX_LLM_BUDGET_USD_PER_DAY')) or settings.max_llm_budget_usd_per_day}/день; "
        f"кандидатов в день: {'без лимита' if cand >= 100000 else cand}",
    ]
    summary = [("Источники", src_lines), ("Каналы", ch_lines), ("Режим работы", mode_lines)]

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

    return templates.TemplateResponse(request, "settings.html", {
        "active": "settings",
        "csrf_token": get_csrf_token(request),
        "msg": msg,
        "rows": rows,
        "overrides": overrides,
        "editable": editable,
        "groups": groups,
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
    elif vtype == "providers":
        order = [w.strip() for w in val.replace("\n", ",").split(",") if w.strip()]
        val = {"order": order, "allow_fallbacks": True} if order else {}
    async with session_scope() as session:
        row = (await session.execute(
            select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
        if row is None:
            session.add(AppSetting(key=key, value=val))
        else:
            row.value = val
        await session.commit()
    return RedirectResponse(f"/settings?msg={quote('сохранено')}", status_code=303)


@router.post("/settings/save_all", dependencies=[Depends(csrf_protect)])
async def settings_save_all(request: Request):
    form = await request.form()
    saved = 0
    async with session_scope() as session:
        for e in EDITABLE:
            if e["attr"] not in form:
                continue
            raw = (form[e["attr"]] or "").strip()
            current = await get_setting(session, e["key"])
            if e["type"] == "providers":
                cur_order = list((current or {}).get("order") or []) \
                    if isinstance(current, dict) else \
                    ([x for x in _providers_to_str(current).split(", ") if x] if current else [])
                order = [w.strip() for w in raw.replace("\n", ",").split(",") if w.strip()]
                if cur_order == order:
                    continue
                val = {"order": order, "allow_fallbacks": True} if order else {}
            elif e["type"] == "list":
                cur_list = list(current or []) if isinstance(current, list) else []
                val = [w.strip() for w in raw.replace("\n", ",").split(",") if w.strip()]
                if cur_list == val:
                    continue
            elif e["type"] == "number":
                try:
                    val = int(raw) if raw.lstrip("-").isdigit() else float(raw)
                except ValueError:
                    continue
                try:
                    if float(current or 0) == float(val):
                        continue
                except (TypeError, ValueError):
                    pass
            else:
                val = raw
                if str(current if current is not None else "") == val:
                    continue
            row = (await session.execute(
                select(AppSetting).where(AppSetting.key == e["key"]))).scalar_one_or_none()
            if row is None:
                session.add(AppSetting(key=e["key"], value=val))
            else:
                row.value = val
            saved += 1
        await session.commit()
    return RedirectResponse(
        f"/settings?msg={quote('сохранено изменений: ' + str(saved))}", status_code=303)


@router.post("/settings/reset", dependencies=[Depends(csrf_protect)])
async def settings_reset(request: Request, key: str = Form(...)):
    async with session_scope() as session:
        row = (await session.execute(
            select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
        if row is not None:
            await session.delete(row)
        await session.commit()
    return RedirectResponse(f"/settings?msg={quote('сброшено к дефолту')}", status_code=303)