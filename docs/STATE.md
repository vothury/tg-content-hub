# TG Content Hub — состояние проекта (STATE)

Живой документ для быстрого входа в контекст. Читается вместе с репозиторием
(публичный: https://github.com/vothury/tg-content-hub). Ассистент при необходимости
подтягивает актуальные файлы через
`https://raw.githubusercontent.com/vothury/tg-content-hub/main/<path>`.

## Назначение
Автоматизированный хаб контент-менеджмента Telegram: читает каналы-источники
(Telethon-аккаунт), дедуплицирует, предфильтр, LLM-классификация по релевантности
каналу, рерайт, ручное ревью владельцем (бот + веб), публикация в целевые каналы
с лимитами/тихими часами, аудит и статистика.

## Доступ и безопасность
- Всё приватно: `api` слушает `127.0.0.1:8000`; доступ только через SSH-туннель
  (PuTTY на ПК, Termius/Termux на Android: локальный 8000 → 127.0.0.1:8000).
- Вход в админку по паролю `ADMIN_PASSWORD` (cookie-сессия, `SECRET_KEY`), CSRF на мутациях.
- Секреты только в `.env` (gitignored). `.gitignore` закрывает: `.env`, `.env.bak`,
  `sessions/*`, `media/`. Репозиторий публичный — кода без секретов, это безопасно.

## Сервисы (docker-compose)
postgres, redis, migrate (alembic), reader, pipeline, bot, scheduler, api.
- reader — Telethon: опрос источников, свежесть/фолбэк, скачивание медиа, посты в БД.
- pipeline — предфильтр → classify → rewrite → статусы ревью; предохранители бюджета.
- bot — aiogram: карточки ревью, одобрить/отклонить/правка ИИ/редактор, режимы публикации.
- scheduler — публикация `publish_jobs` с лимитами/интервалами/тихими часами; уведомления.
- api — FastAPI + Jinja2: веб-админка.

## Конвейер и статусы (app/db/enums.py)
NEW → DEDUPLICATED | PREFILTERED → UNSUITABLE | LLM_CLASSIFYING → CANDIDATE →
REWRITING → AWAITING_REVIEW | NEEDS_MEDIA_REVIEW | NEEDS_MANUAL_REVIEW →
REVISION | MANUAL_EDITING → APPROVED → SCHEDULED/PUBLISHING → PUBLISHED |
REJECTED | FAILED | ARCHIVED.
PublishJobState: queued/scheduled/in_progress/done/failed (+ defer_reason).
DraftOrigin: original/llm_rewrite/llm_revision/manual. PublishMode: now/queue/schedule.

## Конфигурация (три слоя)
1. `.env` → `app/config.py` (дефолты; применяется при `make up`, НЕ при `restart`).
2. Runtime `app_settings` → `app/services/settings.py` (Keys, get/set_setting,
   get_providers) — модели/провайдеры/бюджеты/предфильтр без рестарта; страница «Настройки».
3. `sources.yaml` — источник правды карты контента (styles/targets/sources) →
   `app/services/sources_sync.py` (parse/apply). Веб-редактор в «Контент»:
   черновик + «Применить» (`apply_sources_text`), провенанс и условный sync по хешу —
   `app/services/config_yaml.py` (рестарты НЕ перетирают веб-правки; файл побеждает,
   только если его хеш изменился).

## Веб-админка (app/web)
Страницы: Дашборд `/`, Посты `/posts` (live-обновление `/api/posts`, цвета статусов,
плавающая дата), Карточка `/posts/{id}` (действия по статусу, медиа-превью, блок
«Публикация» с отменой и defer_reason, live-статус), Настройки `/settings`
(редактируемые runtime + все + overrides), Контент `/content` (редактор sources.yaml +
read-only каналы/стили/источники). Шаблоны Jinja2 в `app/web/templates`, стили
`app/web/static/app.css`.

## Ревью — общая логика
`app/services/review.py` используется и ботом, и вебом: approve/reject/media_approve/
retry_manual/apply_ai_revision/apply_manual_edit/create_publish_job.

## Деплой и make
commit (Windows) → push → `git pull` (сервер) → `dos2unix` для `.py` → `make up` →
`make wait-web`. Цели: up/down/logs/ps/migrate/revision/psql/health/sources-sync/
target-list/llm-check/llm-models/wait-web и др. (см. Makefile).

## Миграции (alembic/versions)
0001 initial … 0009 publish_defer_reason. Новые — `make revision m="..."`.

## Договорённости с ассистентом
- HTML/шаблоны отдаются построчно с `#` (иначе Qwen Studio включает предпросмотр и
  теряет текст); остальное — обычными блоками; блоки держать короткими (режутся).
- Пояснения — вне блоков кода. Имя файла и путь — перед блоком.

## Готово (этапы)
1 чтение+медиа · 2 предфильтр · 3 LLM classify/rewrite+бюджет · 4 бот-ревью+свежесть ·
5 публикация+лимиты+уведомления · 6a каркас/вход/дашборд · 6b посты+ревью+медиа+live ·
6c настройки+карта контента+веб-редактор sources.yaml.

## В работе / дальше
- 6d: очередь/публикация и статистика в админке.
- Убрать дубль `admin_password` в config.py.
- `llm_*_max_tokens` оставлены как скрытый предохранитель (вне UI).