# TG Content Hub — состояние проекта (STATE)

Живой документ для быстрого входа в контекст. Читается вместе с репозиторием
(публичный: https://github.com/vothury/tg-content-hub). Ассистент при необходимости
подтягивает актуальные файлы через
`https://raw.githubusercontent.com/vothury/tg-content-hub/main/<path>`.

## Назначение
Автоматизированный хаб контент-менеджмента Telegram: читает каналы-источники
(Telethon-аккаунт), дедуплицирует, предфильтр, LLM-классификация по релевантности
каналу, рерайт, ручное ревью владельцем (бот + веб) ИЛИ автопилот, публикация в
целевые каналы с лимитами/тихими часами, аудит и статистика.

## Доступ и безопасность
- Всё приватно: `api` слушает `127.0.0.1:8000`; доступ только через SSH-туннель
  (PuTTY на ПК, Termius/Termux на Android: локальный 8000 → 127.0.0.1:8000).
- Вход в админку по паролю `ADMIN_PASSWORD` (cookie-сессия, `SECRET_KEY`), CSRF на мутациях.
- Секреты только в `.env` (gitignored). `.gitignore` закрывает: `.env`, `.env.bak`,
  `sessions/*`, `media/`. Репозиторий публичный — кода без секретов, это безопасно.

## Сервисы (docker-compose)
postgres, redis, migrate (alembic), reader, pipeline, bot, scheduler, api.
- reader — Telethon: опрос источников, свежесть/фолбэк, скачивание медиа + pHash,
  аннотирование ссылок, посты в БД.
- pipeline — предфильтр → classify (+canonical) → дедуп → rewrite/clean → статусы
  ревью → автопилот/двойная проверка; предохранители бюджета.
- bot — aiogram: карточки ревью, одобрить/отклонить/правка ИИ/редактор, режимы
  публикации; снятие кнопок после публикации.
- scheduler — публикация `publish_jobs` с лимитами/интервалами/тихими часами; уведомления.
- api — FastAPI + Jinja2: веб-админка.

## Уведомления владельцу
Основной канал — Telegram-бот (карточки + «✅ опубликовано»); на телефоне боту задан
высокий приоритет. PWA Web Push существует, но признан ненадёжным (FCM-доставка) и
используется как необязательный резерв.

## Конвейер и статусы (app/db/enums.py)
NEW → DEDUPLICATED | PREFILTERED → UNSUITABLE | LLM_CLASSIFYING → CANDIDATE →
(дедуп по канону/pHash) → REWRITING → AWAITING_REVIEW | NEEDS_MEDIA_REVIEW |
NEEDS_MANUAL_REVIEW | DOUBLE_CHECK_REVIEW → REVISION | MANUAL_EDITING → APPROVED →
SCHEDULED/PUBLISHING → PUBLISHED | REJECTED | FAILED | ARCHIVED.
- DOUBLE_CHECK_REVIEW — двойная проверка отклонила пост, нужно решение владельца
  (действия ревью доступны; причина в `post.double_check_note`).
PublishJobState: queued/scheduled/in_progress/done/failed (+ defer_reason).
DraftOrigin: original/llm_rewrite/llm_revision/manual. PublishMode: now/queue/schedule.

## Автопилот и двойная проверка (Этап 7)
Per-channel флаги в `sources.yaml` (targets): `autopilot`, `autopilot_min_score`,
`review_if_uncertain`, `double_check`, `double_check_online`,
`double_check_fact_strictness`. Автопилот активен ТОЛЬКО при явном `autopilot: true`.
- score >= порога и suitable → (опц. двойная проверка) → публикация сразу (метка AUTO);
  иначе при `review_if_uncertain` → AWAITING_REVIEW, иначе UNSUITABLE.
- Двойная проверка (`app/services/llm_pipeline.py::_run_double_check`): техническая
  роль (реклама/грубые ошибки/чужая область), НЕ перепроверяет тематику; наследует
  relevance источника. `double_check_online:true` → модель + `:online` (веб-поиск;
  дорого по input-токенам) и отдельная дешёвая `llm.double_check_online_model`.
  Шкала строгости фактов 1-10; при offline модели запрещено утверждать внешние факты.
- Отклон двойной проверкой → DOUBLE_CHECK_REVIEW + note (что ошиблась классификация).
- Лимит дня исчерпан → пост в AWAITING_REVIEW с пометкой (не публикуется).

## Дедупликация (Этап 7+)
`app/services/dedup.py::run_semantic_dedup` после classify. Каскад:
1) точно (source_id, source_message_id); 2) pHash медиа (Хэмминг <= порога) → дубль;
3) каноническая форма текста (`post.canonical_text`, char n-gram косинус >= порога,
   длина канона >= min) → дубль НЕЗАВИСИМО от разных медиа; иначе не дубль.
- Канон генерируется тем же classify-вызовом (поле `canonical`), без доп. затрат.
- Тривиальные посты («🙂») защищены порогом длины канона.
- Первый пост выигрывает; дубль → DEDUPLICATED + details{dup_of, reason}.
- pHash считается при скачивании (PIL dHash; видео — первый кадр ffmpeg),
   `media_items.phash`. Пороги — runtime-настройки `dedup.*` с подсказками в UI.

## Ссылки и подписи (reader)
- `reader._annotate_links`: ссылки/упоминания помечаются `[текст](url)` (UTF-16 офсеты
  Telethon конвертируются), чтобы модель видела их; модели сами решают: подписи
  каналов удаляют, саморекламу (ссылка в теле = контент) отклоняют, полезные ссылки_ok.
- `reader._extract_media`: webpage/link-preview НЕ медиа (не тащим аватарки каналов).
- Подписи («🍿 …», «Подписывайтесь на наш канал [X]») убирают rewrite (правило 7) и
  clean; упоминания внутри смысла предложения сохраняются.

## Классификация (classify-v5)
Категории ok/ads/self_promo/water/off_topic + `canonical`. Промо премьер/релизов в
тему = новость (ok), купленная реклама/промокоды = ads. `llm.classify_verbose`
(дефолт off) включает подробный reason/risks; по умолчанию минимальный ответ.
Каналы без рерайта: черновик = оригинал, подписи срезает clean-вызов (только если
есть ссылки/маркеры, иначе без доп. вызова).

## Конфигурация (три слоя)
1. `.env` → `app/config.py` (дефолты; применяется при `make up`, НЕ при `restart`).
2. Runtime `app_settings` → `app/services/settings.py` (Keys, get/set_setting,
   get_providers) — модели/провайдеры/бюджеты/предфильтр/автопилот/дедуп без рестарта;
   страница «Настройки» (карточки «Модель → Провайдеры» пары, подсказки-hint).
3. `sources.yaml` — источник правды карты контента (styles/targets/sources) →
   `app/services/sources_sync.py` (parse/apply). Веб-редактор в «Контент»:
   черновик + «Применить» (`apply_sources_text`), провенанс и условный sync по хешу —
   `app/services/config_yaml.py` (рестарты НЕ перетирают веб-правки; файл побеждает,
   только если его хеш изменился).

## Веб-админка (app/web)
Страницы: Дашборд `/`, Посты `/posts` (live-обновление `/api/posts`, цвета статусов,
плавающая дата), Карточка `/posts/{id}` (действия по статусу incl. DOUBLE_CHECK_REVIEW,
медиа-превью, блок «Публикация» с отменой и defer_reason, метка 🤖 автопилот,
⛔-причина двойной проверки, live-статус), Настройки `/settings` (редактируемые
runtime + все + overrides), Контент `/content` (редактор sources.yaml + read-only
каналы/стили/источники). Шаблоны Jinja2 в `app/web/templates`, стили
`app/web/static/app.css`.

## Ревью — общая логика
`app/services/review.py` используется и ботом, и вебом: approve/reject/media_approve/
retry_manual/apply_ai_revision/apply_manual_edit/create_publish_job. Статусы
AWAITING_REVIEW и DOUBLE_CHECK_REVIEW дают полный набор действий.

## Деплой и make
commit (Windows) → push → `git pull` (сервер) → `dos2unix` для `.py` → `make up` →
`make wait-web`. Цели: up/down/logs/ps/migrate/revision/psql/health/sources-sync/
target-list/llm-check/llm-models/wait-web и др. (см. Makefile). Сборка ~15-20 мин —
правки группируем в один коммит/сборку.

## Миграции (alembic/versions)
0001 initial … 0009 publish_defer_reason · 0010 autopilot+double_check ·
0011 double_check online/strictness · 0012 dedup (canonical_text, phash).
Новые — `make revision m="..."`.

## Договорённости с ассистентом
- HTML/шаблоны отдаются построчно с `#` (иначе Qwen Studio включает предпросмотр и
  теряет текст); остальное — обычными блоками; блоки держать короткими (режутся).
- Правки функций — ПОЛНОЙ заменой функции или точной привязкой «после строки X»;
  без расплывчатых «добавьте в функцию Y».
- Пояснения — вне блоков кода. Имя файла и путь — перед блоком.

## Готово (этапы)
1 чтение+медиа · 2 предфильтр · 3 LLM classify/rewrite+бюджет · 4 бот-ревью+свежесть ·
5 публикация+лимиты+уведомления · 6a каркас/вход/дашборд · 6b посты+ревью+медиа+live ·
6c настройки+карта контента+веб-редактор sources.yaml · 6d очередь/статистика ·
7 автопилот + двойная проверка + семантическая дедупликация + ссылки/подписи.

## В работе / дальше
- Наблюдение за автопилотом/дедупом на реальных постах; подкрутка порогов dedup.*.
- Убрать дубль `admin_password` в config.py.
- `llm_*_max_tokens` оставлены как скрытый предохранитель (вне UI).