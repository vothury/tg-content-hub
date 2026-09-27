import json

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.config import settings
from app.db.models import AppSetting, Source, StyleProfile, TargetChannel
from app.db.session import session_scope
from app.services.settings import Keys, get_setting, repair_list
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
        return ", ".join(repair_list(v.get("order") or []))
    if isinstance(v, list):
        return ", ".join(repair_list(v))
    return str(v)


def _list_to_str(v) -> str:
    """Список в человекочитаемую строку для input (без repr-скобок и кавычек)."""
    if v is None or v == "":
        return ""
    return ", ".join(repair_list(v))


EDITABLE = [
    # --- Модели и провайдеры стадий ---
    {"key": _k("CLASSIFY_MODEL", "llm.classify_model"), "label": "Модель классификации", "attr": "classify_model", "type": "text"},
    {"key": _k("CLASSIFY_PROVIDERS", "llm.classify_providers"), "label": "Провайдеры классификации (через запятую)", "attr": "classify_providers", "type": "providers"},
    {"key": _k("AGGREGATE_MODEL", "llm.aggregate_model"), "label": "Модель агрегатора (технические каналы)", "attr": "aggregate_model", "type": "text",
     "hint": "Отдельная модель или список для фильтра агрегаторов; пусто = модель классификации. Сюда ставят дешёвое/пул: openrouter/free."},
    {"key": _k("REWRITE_MODEL", "llm.rewrite_model"), "label": "Модель рерайта", "attr": "rewrite_model", "type": "text"},
    {"key": _k("REWRITE_PROVIDERS", "llm.rewrite_providers"), "label": "Провайдеры рерайта (через запятую)", "attr": "rewrite_providers", "type": "providers"},
    {"key": _k("REVISION_MODEL", "llm.revision_model"), "label": "Модель правки", "attr": "revision_model", "type": "text"},
    {"key": _k("REVISION_PROVIDERS", "llm.revision_providers"), "label": "Провайдеры правки (через запятую)", "attr": "revision_providers", "type": "providers"},
    {"key": _k("PREFILTER_MODEL", "llm.prefilter_model"), "label": "Модель очистки (clean)", "attr": "prefilter_model", "type": "text"},
    {"key": _k("PREFILTER_PROVIDERS", "llm.prefilter_providers"), "label": "Провайдеры очистки (через запятую)", "attr": "prefilter_providers", "type": "providers"},
    {"key": _k("DOUBLE_CHECK_MODEL", "llm.double_check_model"), "label": "Модель двойной проверки", "attr": "double_check_model", "type": "text"},
    {"key": _k("DOUBLE_CHECK_PROVIDERS", "llm.double_check_providers"), "label": "Провайдеры двойной проверки (через запятую)", "attr": "double_check_providers", "type": "providers"},
    {"key": _k("DOUBLE_CHECK_ONLINE_MODEL", "llm.double_check_online_model"), "label": "Модель онлайн-фактчекинга", "attr": "double_check_online_model", "type": "text"},
    {"key": _k("DOUBLE_CHECK_ONLINE_PROVIDERS", "llm.double_check_online_providers"), "label": "Провайдеры онлайн-фактчекинга (через запятую)", "attr": "double_check_online_providers", "type": "providers"},
    {"key": _k("CLEAN_FALLBACK_MODEL", "llm.clean_fallback_model"), "label": "Модель очистки подписей (страховка)", "attr": "clean_fallback_model", "type": "text",
     "hint": "Сильная модель для второй попытки, если дешёвая дала непроверяемый план; пусто = модель двойной проверки."},
    {"key": _k("LLM_FALLBACK_MODELS", "llm.fallback_models"), "label": "Запасные модели (через запятую)", "attr": "llm_fallback_models", "type": "list",
     "hint": "Пробуются по очереди, если основная вернула мусор или ответ модели модерации (Nemotron Content Safety). Указывайте конкретные slug'и, например deepseek/deepseek-chat-v3-0324:free."},
    {"key": _k("LLM_SENSITIVE_MODEL", "llm.sensitive_model"), "label": "LLM: модель для чувствительных постов", "attr": "llm_sensitive_model", "type": "text",
     "hint": "Slug модели без жёстких модерационных обрывов (например grok-…); пусто = чувствительные посты требуют ручного подтверждения."},
    {"key": _k("LLM_RESPONSE_LANG", "llm.response_lang"), "label": "LLM: язык ответов моделей", "attr": "llm_response_lang", "type": "text",
     "hint": "ru = строковые значения (reason, note, draft) на русском; en = на английском (дешевле). Рассуждения всегда на английском; canonical — всегда в языке исходного текста."},
    # --- Бюджеты и лимиты LLM ---
    {"key": _k("MAX_LLM_BUDGET_USD_PER_DAY", "limits.max_llm_budget_usd_per_day"), "label": "Бюджет LLM, $/день", "attr": "max_llm_budget_usd_per_day", "type": "number"},
    {"key": _k("MAX_CANDIDATES_PER_DAY", "limits.max_candidates_per_day"), "label": "Лимит кандидатов в день", "attr": "max_candidates_per_day", "type": "number",
     "hint": "Сколько постов может дойти до стадии CANDIDATE за сутки (предохранитель конвейера)."},
    {"key": _k("LLM_REASONING_MAX_TOKENS", "llm.reasoning_max_tokens"), "label": "LLM: бюджет reasoning", "attr": "llm_reasoning_max_tokens", "type": "number",
     "hint": "Максимум токенов внутренних рассуждений (classify / double-check offline); защищает от зацикливания."},
    {"key": _k("LLM_REASONING_REWRITE", "llm.reasoning_rewrite"), "label": "LLM: бюджет reasoning (рерайт)", "attr": "llm_reasoning_rewrite", "type": "number",
     "hint": "Рерайт под стиль и правка ИИ: творческая задача, бюджет выше базового."},
    {"key": _k("LLM_REASONING_ONLINE_CHECK", "llm.reasoning_online_check"), "label": "LLM: бюджет reasoning (фактчекинг)", "attr": "llm_reasoning_online_check", "type": "number",
     "hint": "Двойная проверка с поиском в интернете: нужен запас на сопоставление фактов."},
    {"key": _k("LLM_REASONING_SMALL", "llm.reasoning_small"), "label": "LLM: бюджет reasoning (мелкие задачи)", "attr": "llm_reasoning_small", "type": "number",
     "hint": "Чистка подписей, перевод причины, подтверждение дубля: рассуждения почти не нужны."},
    {"key": _k("LLM_DOUBLE_CHECK_MAX_TOKENS", "llm.double_check_max_tokens"), "label": "LLM: max_tokens двойной проверки", "attr": "llm_double_check_max_tokens", "type": "number",
     "hint": "Токены на финальный ответ двойной проверки ПОСЛЕ рассуждений; итоговый лимит = это значение + бюджет reasoning. Ответ — короткий JSON, 800 достаточно с запасом."},
    # --- Префильтр ---
    {"key": _k("PREFILTER_MIN_TEXT_LEN", "prefilter.min_text_len"), "label": "Префильтр: мин. длина текста", "attr": "prefilter_min_text_len", "type": "number",
     "hint": "Короче этого текст без медиа отклоняется; посты с медиа и коротким текстом уходят на визуальное ревью."},
    {"key": _k("PREFILTER_BLACKLIST_WORDS", "prefilter.blacklist_words"), "label": "Блэклист слов (через запятую)", "attr": "prefilter_blacklist_words", "type": "list"},
    {"key": _k("PREFILTER_SELFPROMO_PATTERNS", "prefilter.selfpromo_patterns"), "label": "Префильтр: маркеры самопиара источника", "attr": "prefilter_selfpromo_patterns", "type": "list",
     "hint": "Фразы типа «нам на канал», «залил нам», «у нас на канале»: пост отсекается технически, до вызова модели."},
    {"key": _k("PREFILTER_EVENT_MARKERS", "prefilter.event_markers"), "label": "Префильтр: маркеры анонсов мероприятий", "attr": "prefilter_event_markers", "type": "list",
     "hint": "«регистрация», «прямой эфир», «вебинар» и т.п.; срабатывает только вместе с площадкой регистрации либо датой и временем."},
    {"key": _k("PREFILTER_EVENT_DOMAINS", "prefilter.event_domains"), "label": "Префильтр: площадки регистрации", "attr": "prefilter_event_domains", "type": "list",
     "hint": "Домены записи на мероприятия: timepad.ru, webinar.ru, zoom.us и т.п."},
    {"key": _k("PREFILTER_SENSITIVE_WORDS", "prefilter.sensitive_words"), "label": "Префильтр: чувствительные слова (через запятую)", "attr": "prefilter_sensitive_words", "type": "list",
     "hint": "Слова, на которых провайдеры обрывают рассуждения модели. Пост с таким словом не идёт в семантику: либо ручное решение, либо модель из «LLM: модель для чувствительных постов»."},
    # --- Контроль цен моделей ---
    {"key": _k("PRICE_WATCH_ENABLED", "price_watch.enabled"), "label": "Цены моделей: контроль включён", "attr": "price_watch_enabled", "type": "number",
     "hint": "1 = раз в сутки сверять цены используемых моделей с каталогом OpenRouter."},
    {"key": _k("PRICE_ALERT_PCT", "price_watch.alert_pct"), "label": "Цены моделей: порог предупреждения, %", "attr": "price_alert_pct", "type": "number",
     "hint": "Насколько должна измениться цена (input или output), чтобы показать баннер."},
    {"key": _k("PRICE_WATCH_INTERVAL_HOURS", "price_watch.interval_hours"), "label": "Цены моделей: интервал проверки, часов", "attr": "price_watch_interval_hours", "type": "number",
     "hint": "Как часто опрашивать каталог OpenRouter (запрос бесплатный)."},
    {"key": _k("PRICE_HISTORY_MIN_CHANGE_PCT", "price_watch.history_min_change_pct"), "label": "Цены моделей: порог записи истории, %", "attr": "price_history_min_change_pct", "type": "number",
     "hint": "Изменения мельче этого процента в историю не пишутся: защита от раздувания таблицы на волатильных ценах."},
    {"key": _k("PRICE_WATCH_SCOPE", "price_watch.scope"), "label": "Цены моделей: разрез провайдеров", "attr": "price_watch_scope", "type": "text",
     "hint": "pinned = только провайдеры, закреплённые в настройках стадий; cheapest = самый дешёвый доступный; all = все эндпоинты; off = только агрегированная цена."},
    # --- Публикация ---
    {"key": _k("PUBLISH_DUP_RECAP_WINDOW_HOURS", "publish.dup_recap_window_hours"), "label": "Публикация: окно «ранее писали», часов", "attr": "publish_dup_recap_window_hours", "type": "number",
     "hint": "Сколько часов с первой публикации темы считать её «той же темой»; старше — новая тема без блока."},
    {"key": _k("PUBLISH_RESTORE_LINKS", "publish.restore_links"), "label": "Публикация: возвращать съеденные ссылки", "attr": "publish_restore_links", "type": "number",
     "hint": "1 = если в одобренном посте не осталось ни одной ссылки, а в оригинале они были, добавить строку «Подробнее: url»."},
    {"key": _k("PUBLISH_MAX_MEDIA_MB", "publish.max_media_mb"), "label": "Публикация: лимит размера медиа, МБ", "attr": "publish_max_media_mb", "type": "number",
     "hint": "Проверяется ДО отправки в Telegram: и каждый файл, и суммарный размер альбома. Лимит Bot API — 50 МБ."},
    {"key": _k("PUBLISH_MEDIA_COMPRESS", "publish.media_compress"), "label": "Публикация: сжимать видео ffmpeg", "attr": "publish_media_compress", "type": "number",
     "hint": "1 = если видео больше лимита, пересобрать его под целевой размер (нагрузка на CPU; на слабом сервере лучше держать 0)."},
    {"key": _k("PUBLISH_COMPRESS_TARGET_MB", "publish.compress_target_mb"), "label": "Публикация: цель сжатия, МБ", "attr": "publish_compress_target_mb", "type": "number",
     "hint": "Целевой размер видео после сжатия (не больше лимита)."},
    {"key": _k("PUBLISH_COMPRESS_MAX_SIDE", "publish.compress_max_side"), "label": "Публикация: макс. сторона кадра при сжатии", "attr": "publish_compress_max_side", "type": "number",
     "hint": "Например 1280 = не шире 720p. Меньше — сильнее сжатие и меньше размер."},
    {"key": _k("PUBLISH_SKIP_OVERSIZED", "publish.skip_oversized"), "label": "Публикация: пропускать медиа больше лимита", "attr": "publish_skip_oversized", "type": "number",
     "hint": "1 = опубликовать пост без «тяжёлого» файла вместо ошибки; 0 = пост не публикуется."},
    {"key": _k("PUBLISH_STRIP_SOURCE_DECOR", "publish.strip_source_decor"), "label": "Публикация: убирать хэштеги и ссылки-тизеры", "attr": "publish_strip_source_decor", "type": "number",
     "hint": "1 = удалять хэштеги источника и строки «Подробнее тут/читайте на dzen/pikabu/vk/чат/boost»; ссылки на официальный контент (трейлер, IMDb, сайт студии) сохраняются."},
    # --- Курирование ---
    {"key": _k("CURATION_ENABLED", "curation.enabled"), "label": "Курирование: включено", "attr": "curation_enabled", "type": "number",
     "hint": "1 = принимать пересылки из каналов-приёмников с подписью-списком целевых каналов."},
    {"key": _k("CURATION_TARGET_ALIASES", "curation.target_aliases"), "label": "Курирование: алиасы целевых каналов", "attr": "curation_target_aliases", "type": "text",
     "hint": "Пары «канал=алиас» или «канал - алиас» через запятую (разделители пары: =, :, - или пробел; разделители пар: запятая, ;, перенос)."},
    {"key": _k("CURATION_INBOX_CHANNELS", "curation.inbox_channels"), "label": "Курирование: каналы-приёмники (через запятую)", "attr": "curation_inbox_channels", "type": "list",
     "hint": "Ваши каналы, куда вы пересылаете понравившиеся посты (например go_tests). Обрабатываются ТОЛЬКО они."},
    # --- Виртуальная редакция ---
    {"key": _k("EDITORIAL_ENABLED", "editorial.enabled"), "label": "Редакция: включена", "attr": "editorial_enabled", "type": "number",
     "hint": "1 = циклы виртуальной редакции работают; 0 = приостановлены."},
    {"key": _k("EDITORIAL_CYCLE_TIMES", "editorial.cycle_times"), "label": "Редакция: времена циклов", "attr": "editorial_cycle_times", "type": "text",
     "hint": "Через запятую ЧЧ:ММ; один цикл = журналист → главред → сбор/текст."},
    {"key": _k("EDITORIAL_PUBLISH_WINDOWS", "editorial.publish_windows"), "label": "Редакция: окна публикации", "attr": "editorial_publish_windows", "type": "text",
     "hint": "Интервалы ЧЧ:ММ-ЧЧ:ММ через запятую; внутри окна выбирается случайная минута."},
    {"key": _k("EDITORIAL_PREPARED_HOURS", "editorial.prepared_hours"), "label": "Редакция: память тем, часов", "attr": "editorial_prepared_hours", "type": "number",
     "hint": "Сколько часов главред помнит темы в работе/готовые (со статусами)."},
    {"key": _k("EDITORIAL_PUBLISHED_HOURS", "editorial.published_hours"), "label": "Редакция: память опубликованного, часов", "attr": "editorial_published_hours", "type": "number",
     "hint": "Сколько часов главред помнит заголовки опубликованных материалов."},
    {"key": _k("EDITORIAL_WRITER_MODE", "editorial.writer_mode"), "label": "Редакция: кто пишет", "attr": "editorial_writer_mode", "type": "text",
     "hint": "journalist (вариант A, дешевле) или model (вариант B, editorial_writer_model)."},
    {"key": _k("EDITORIAL_WRITER_MODEL", "editorial.writer_model"), "label": "Редакция: модель автора (B)", "attr": "editorial_writer_model", "type": "text",
     "hint": "Используется только при writer_mode=model."},
    {"key": _k("EDITORIAL_AUTO_PUBLISH", "editorial.auto_publish"), "label": "Редакция: автопубликация", "attr": "editorial_auto_publish", "type": "number",
     "hint": "0 = статья ждёт одобрения владельца; 1 = публикация по окнам без ревью."},
    {"key": _k("EDITORIAL_POST_MAX_CHARS", "editorial.post_max_chars"), "label": "Редакция: макс. знаков поста", "attr": "editorial_post_max_chars", "type": "number",
     "hint": "Целевой предел 250-400, жёсткий максимум — это значение."},
    {"key": _k("EDITORIAL_REWRITE_MAX_PER_DAY", "editorial.rewrite_max_per_day"), "label": "Редакция: рерайтов в день", "attr": "editorial_rewrite_max_per_day", "type": "number",
     "hint": "Ограничение доли рерайтов, чтобы канал оставался аналитикой, а не лентой."},
    {"key": _k("EDITORIAL_BUDGET_USD_PER_DAY", "editorial.budget_usd_per_day"), "label": "Редакция: бюджет $/день", "attr": "editorial_budget_usd_per_day", "type": "number",
     "hint": "Отдельный дневной потолок расходов редакции (поверх общего бюджета)."},
    {"key": _k("EDITORIAL_CHIEF_MODEL", "editorial.chief_model"), "label": "Редакция: модель главреда", "attr": "editorial_chief_model", "type": "text",
     "hint": "Пусто = модель ревизии по умолчанию; нужна сильная reasoning-модель."},
    {"key": _k("EDITORIAL_JOURNALIST_MODEL", "editorial.journalist_model"), "label": "Редакция: модель журналиста", "attr": "editorial_journalist_model", "type": "text",
     "hint": "Пусто = дешёвая prefilter-модель."},
    {"key": _k("EDITORIAL_BROWSE_MODEL", "editorial.browse_model"), "label": "Редакция: browse-модель (ходит по url)", "attr": "editorial_browse_model", "type": "text",
     "hint": "Модель с веб-инструментом для чтения страниц; пусто = модель журналиста + ':online'."},
    {"key": _k("EDITORIAL_BROWSE_ENABLED", "editorial.browse_enabled"), "label": "Редакция: browse-модель включена", "attr": "editorial_browse_enabled", "type": "number",
     "hint": "1 = при недоступности страницы вызывать модель с веб-инструментом (фикс ~$0.007 за выход в интернет); 0 = такие источники пропускаются."},
    # --- Агрегатор ---
    {"key": _k("AGGREGATE_ACCEPT_DEFAULT", "aggregate.accept_default"), "label": "Агрегатор: одобрять (дефолт)", "attr": "aggregate_accept_default", "type": "text",
     "hint": "Что считать релевантным для технических каналов, если у канала не задан свой aggregate_accept."},
    {"key": _k("AGGREGATE_MAX_TOKENS", "llm.aggregate_max_tokens"), "label": "Агрегатор: max_tokens ответа", "attr": "llm_aggregate_max_tokens", "type": "number",
     "hint": "Бюджет токенов на финальный JSON тематического фильтра. Мало — JSON обрезается лимитом и пост ротится на запасные модели; 1000 достаточно с запасом. Итоговый лимит = это значение + бюджет reasoning (мелкие задачи)."},
    {"key": _k("AGGREGATE_REJECT_DEFAULT", "aggregate.reject_default"), "label": "Агрегатор: отклонять (дефолт)", "attr": "aggregate_reject_default", "type": "text",
     "hint": "Что отклонять для технических каналов, если у канала не задан свой aggregate_reject."},
    # --- Reader / автопилот ---
    {"key": _k("MAX_MEDIA_DOWNLOAD_MB", "reader.max_media_download_mb"), "label": "Макс. размер медиа, МБ", "attr": "max_media_download_mb", "type": "number"},
    {"key": _k("READER_DEFAULT_SOURCE_INTERVAL_SEC", "reader.default_source_interval_sec"), "label": "Интервал опроса источника, сек", "attr": "reader_default_source_interval_sec", "type": "number"},
    {"key": _k("AUTOPILOT_MIN_SCORE", "autopilot.min_score"), "label": "Автопилот: мин. score", "attr": "autopilot_min_score", "type": "number"},
    # --- Дедупликация ---
    {"key": _k("DEDUP_WINDOW_DAYS", "dedup.window_days"), "label": "Дедуп: окно, дней", "attr": "dedup_window_days", "type": "number",
     "hint": "За сколько дней искать дубли среди постов того же целевого канала."},
    {"key": _k("DEDUP_CANONICAL_COSINE_MIN", "dedup.canonical_cosine_min"), "label": "Дедуп: порог похожести текста", "attr": "dedup_canonical_cosine_min", "type": "number",
     "hint": "0..1 — косинус по символьным n-граммам канонической формы; выше = строже."},
    {"key": _k("DEDUP_CANONICAL_CONTAINMENT_MIN", "dedup.canonical_containment_min"), "label": "Дедуп: порог включения", "attr": "dedup_canonical_containment_min", "type": "number",
     "hint": "0..1 — доля n-грамм короткого канона, входящих в длинный; строже косинуса, ловит случай «один список полнее другого»."},
    {"key": _k("DEDUP_FACT_CONTAINMENT_MIN", "dedup.fact_containment_min"), "label": "Дедуп: порог фактовых якорей", "attr": "dedup_fact_containment_min", "type": "number",
     "hint": "0..1 — перекрытие множества «якорей» (названия в «…», имена, даты); не зависит от порядка слов."},
    {"key": _k("DEDUP_CANONICAL_MIN_LEN", "dedup.canonical_min_len"), "label": "Дедуп: мин. длина канона", "attr": "dedup_canonical_min_len", "type": "number",
     "hint": "Короче этого канон игнорируется (защита от тривиальных постов вроде «🙂»)."},
    {"key": _k("DEDUP_PHASH_MAX_DISTANCE", "dedup.phash_max_distance"), "label": "Дедуп: порог pHash", "attr": "dedup_phash_max_distance", "type": "number",
     "hint": "Расстояние Хэмминга 0..64 для «то же изображение»; меньше = строже."},
    {"key": _k("DEDUP_LUMA_MAX_DIFF", "dedup.luma_max_diff"), "label": "Дедуп: допуск яркости", "attr": "dedup_luma_max_diff", "type": "number",
     "hint": "0..255 — макс. разница средней яркости изображений для media-матча; чёрное и белое не совпадут."},
    {"key": _k("DEDUP_CONFIRM_MODEL", "dedup.confirm_model"), "label": "Дедуп: модель сверки текстов", "attr": "dedup_confirm_model", "type": "text",
     "hint": "Модель, которая по полным текстам решает, дубль это или разные факты; пусто = модель очистки (clean)."},
    {"key": _k("DEDUP_CONFIRM_PROVIDERS", "dedup.confirm_providers"), "label": "Дедуп: провайдеры сверки (через запятую)", "attr": "dedup_confirm_providers", "type": "providers",
     "hint": "Пусто = авто-роутинг OpenRouter."},
    {"key": _k("DEDUP_MAX_COMPARE", "dedup.max_compare"), "label": "Дедуп: максимум сравнений", "attr": "dedup_max_compare", "type": "number",
     "hint": "Сколько недавних постов канала сравнивать (ограничивает нагрузку)."},
]

ATTR_TO_KEY = {c["attr"]: c["key"] for c in EDITABLE}


GROUPS = [
    ("Виртуальная редакция", ("editorial_",), ()),
    ("Цены моделей (OpenRouter)", ("price_",), ()),
    ("Курирование", ("curation_",), ()),
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
            elif e["type"] == "list":
                current = _list_to_str(current)
                default = _list_to_str(default)
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
        + ("; инструкции модели: да" if c.llm_instructions else "")
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
        val = repair_list(val)
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
                cur_list = repair_list(current)
                val = repair_list(raw)
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