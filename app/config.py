"""Централизованная конфигурация. Все секреты — только из окружения."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Среда
    environment: str = "dev"
    log_level: str = "INFO"

    # Хранилища
    database_url: str = "postgresql+asyncpg://content_hub:content_hub@postgres:5432/content_hub"
    redis_url: str = "redis://redis:6379/0"
    media_dir: str = "media"

    # Telegram: чтение источников (отдельный аккаунт)
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    reader_session_path: str = "sessions/reader"
    reader_poll_interval_sec: int = 30
    reader_backfill_limit: int = 20

    # Telegram: бот (публикация и ревью)
    bot_token: str = ""
    allowed_owner_ids: list[int] = Field(default_factory=list)

    # LLM / OpenRouter
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    prefilter_model: str = "deepseek/deepseek-v4-flash-0731"
    classify_model: str = "deepseek/deepseek-v4-flash-0731"
    rewrite_model: str = "openai/gpt-5.6-luna"
    revision_model: str = ""  # пусто = модель рерайта

    # Предохранители
    max_llm_budget_usd_per_day: float = 1.0
    max_candidates_per_day: int = 100000

    # Админка (этап 6)
    admin_password: str = ""

    # Читатель: интервал опроса источника по умолчанию (если не задан у источника)
    reader_default_source_interval_sec: int = 120
    # Предохранитель: медиа крупнее не скачиваются (Этап 1)
    max_media_download_mb: int = 100

    # Предфильтр (Этап 2)
    prefilter_min_text_len: int = 0
    prefilter_blacklist_words: list[str] = Field(default_factory=list)
    
    # Анонсы мероприятий: маркер + (площадка регистрации ИЛИ дата+время) = отклонить технически
    prefilter_event_markers: list = [
        "регистрация по ссылке", "прямой эфир",
        "вебинар", "мастер-класс", "онлайн-встреча", "онлайн-эфир", "офлайн-встреча",
        "участие бесплатное", "места ограничены", "подключайтесь", "ждём вас",
        "приглашаем на", "эфир состоится", "начало в",
    ]
    prefilter_event_domains: list = [
        "timepad.ru", "webinar.ru", "meetup.com", "eventbrite", "zoom.us",
        "mts-link", "vk.com/app", "youtube.com/live",
    ]
    
    # Технический стоп-фильтр самопиара источника (срабатывает до вызова модели)
    prefilter_selfpromo_patterns: list = [
        "нам на канал", "залил нам", "залили нам", "мы залили", "мы выложили",
        "у нас на канале", "на нашем канале", "смотрите у нас", "читайте у нас",
        "мы добавили", "наша фильмотека", "наша подборка", "подписывайтесь",
    ]

    # LLM (Этап 3)
    openrouter_request_timeout_sec: int = 90
    llm_classify_max_tokens: int = 8000
    llm_rewrite_max_tokens: int = 15000
    llm_reasoning_max_tokens: int = 1000   # бюджет reasoning для classify/double-check
    llm_reasoning_rewrite: int = 2000      # рерайт под стиль / правка ИИ
    llm_reasoning_online_check: int = 2500 # двойная проверка с фактчекингом в интернете
    llm_reasoning_small: int = 300         # чистка, перевод, подтверждение дубля
    publish_dup_recap_window_hours: int = 24
    llm_double_check_max_tokens: int = 800 # токены финального ответа двойной проверки (поверх reasoning)
    clean_fallback_model: str = ""   # очистка подписей, 2-я попытка; пусто = модель двойной проверки
    llm_fallback_models: list = []   # запасные модели при сбое основной (модель модерации, мусорный JSON)
    # Контроль цен моделей OpenRouter
    price_watch_enabled: int = 1
    price_alert_pct: float = 25.0        # на сколько % должна вырасти цена, чтобы предупредить
    price_watch_interval_hours: int = 8
    price_history_min_change_pct: float = 1.0  # мельче — в историю не пишем (защита от волатильности)
    # Наблюдение цен по провайдерам: pinned | cheapest | all | off
    price_watch_scope: str = "pinned"
    openrouter_management_key: str = ""   # только для API цен провайдеров (не для инференса)
    publish_dup_recap_window_hours: int = 24  # окно «ранее писали» для дублей-агрегаторов
    publish_restore_links: int = 0   # 0 = выкл (ссылки и так публикуются кликабельными); 1 = страховка только для информационных ссылок
    # Публикация медиа: лимиты размера и опциональное сжатие видео (ffmpeg)
    publish_max_media_mb: int = 50       # лимит на файл и на суммарный размер альбома
    publish_media_compress: int = 0      # 1 = сжимать видео под лимит (требует CPU)
    publish_compress_target_mb: int = 45 # целевой размер после сжатия
    publish_compress_max_side: int = 1280  # ограничение большей стороны кадра
    publish_skip_oversized: int = 0      # 1 = публиковать без «тяжёлого» медиа, а не падать
    publish_strip_source_decor: int = 1  # 1 = убирать хэштеги и ссылки-тизеры источника при публикации
    # Курирование: приём пересылок с указанием целевых каналов
    curation_enabled: int = 1
    curation_inbox_channels: list = []   # например: go_tests
    # Чувствительная лексика: слова, из-за которых провайдеры обрывают рассуждения
    prefilter_sensitive_words: list = ["порно"]
    llm_sensitive_model: str = ""   # пусто = чувствительные посты только вручную; иначе — модель без обрывов
    llm_response_lang: str = "ru"   # ru|en: язык строковых значений ответов моделей

    # ---- Виртуальная редакция ----
    editorial_enabled: int = 1
    editorial_cycle_times: str = "07:30,12:30,17:30"          # journalist→chief→gather одним циклом
    editorial_publish_windows: str = "08:00-10:00,12:00-15:00,17:00-20:00"
    editorial_prepared_hours: int = 5                          # память: темы в работе/готовы
    editorial_published_hours: int = 36                        # память: опубликованное
    editorial_writer_mode: str = "journalist"                  # journalist | model
    editorial_writer_model: str = ""                           # пусто = journalist-модель
    editorial_auto_publish: int = 0                            # 0 = ручное ревью владельца
    editorial_post_max_chars: int = 500
    editorial_rewrite_max_per_day: int = 1
    editorial_budget_usd_per_day: float = 1.5
    editorial_chief_model: str = ""                            # пусто = effective_revision_model
    editorial_journalist_model: str = ""                       # пусто = prefilter-модель
    editorial_browse_model: str = ""                           # пусто = модель журналиста + ":online"
    editorial_browse_enabled: int = 0   # 0 = не вызывать browse-модель (фикс-плата за выход в интернет)
    aggregate_accept_default: str = "новости и факты по теме канала: конкретика (кто, что, где, когда), цифры, решения и события"
    aggregate_reject_default: str = "реклама и партнёрские интеграции, самореклама, развлечения и мемы, вторичный рынок и аренда, бытовые и тарифные новости, объекты и сюжеты вне тематики канала"

    # Как часто пайплайн пересматривает «застрявшие» посты при пустой очереди
    pipeline_rescan_interval_sec: int = 60

    # Предпочтения провайдеров OpenRouter (JSON; пусто = авто-маршрутизация)
    # Пример: {"order": ["OpenAI"], "allow_fallbacks": false, "quantizations": ["fp8"]}
    classify_providers: str = ""
    rewrite_providers: str = ""
    revision_providers: str = ""
    prefilter_providers: str = ""
    double_check_providers: str = ""
    double_check_online_providers: str = ""

    # Автопилот (Этап 7)
    autopilot_min_score: int = 8
    double_check_model: str = ""  # пусто = модель правки
    double_check_online_model: str = ""  # модель для online-фактчекинга (дешевле); пусто = модель double_check
    classify_verbose: bool = False  # подробный вердикт (reason/risks) — дороже и медленнее
    double_check_fact_strictness: int = 4  # 1-10, если не задано у канала

    # Ревью (Этап 4): период поиска постов без отправленной карточки
    review_poll_interval_sec: int = 30

    # Политика свежести постов (Этап 4+)
    reader_fresh_window_min: int = 60          # окно свежести, минуты
    reader_fallback_count: int = 2             # если свежих нет: взять последних
    reader_fallback_max_age_hours: int = 48    # но не старше этого возраста

    # Публикация (Этап 5)
    owner_timezone: str = "UTC"           # часовой пояс владельца (например, Europe/Moscow)
    scheduler_poll_interval_sec: int = 15

    # Веб-админка (Этап 6)
    admin_password: str = ""
    secret_key: str = ""
    
    # Дедупликация (Этап 7+)
    dedup_window_days: int = 7
    dedup_phash_max_distance: int = 4      # Хэмминг 0..64 для «то же изображение»
    dedup_luma_max_diff: int = 48      # допуск разницы средней яркости для media-матча
    dedup_confirm_model: str = ""        # пусто = модель очистки (prefilter)
    dedup_confirm_providers: dict = {}   # пусто = авто-роутинг OpenRouter
    dedup_canonical_min_len: int = 30      # короче — канон игнорируем (защита от «🙂»)
    dedup_canonical_cosine_min: float = 0.60
    dedup_canonical_containment_min: float = 0.75  # доля n-грамм короткого канона в длинном
    dedup_max_compare: int = 200
    dedup_fact_containment_min: float = 0.70

    @property
    def effective_revision_model(self) -> str:
        return self.revision_model or self.rewrite_model

    @property
    def effective_double_check_model(self) -> str:
        return self.double_check_model or self.effective_revision_model


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()