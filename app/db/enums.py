"""Все перечисления статусов и типов. Значения совпадают с БД."""
import enum


class SourceKind(str, enum.Enum):
    EXTERNAL = "external"
    TEST = "test"


class PostStatus(str, enum.Enum):
    NEW = "NEW"
    DEDUPLICATED = "DEDUPLICATED"
    PREFILTERED = "PREFILTERED"
    UNSUITABLE = "UNSUITABLE"
    LLM_CLASSIFYING = "LLM_CLASSIFYING"
    CANDIDATE = "CANDIDATE"
    REWRITING = "REWRITING"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    NEEDS_MEDIA_REVIEW = "NEEDS_MEDIA_REVIEW"
    # Добавлено к списку ТЗ: сбой/непарсируемый ответ LLM -> ручной разбор
    NEEDS_MANUAL_REVIEW = "NEEDS_MANUAL_REVIEW"
    DOUBLE_CHECK_REVIEW = "DOUBLE_CHECK_REVIEW"
    REVISION = "REVISION"
    MANUAL_EDITING = "MANUAL_EDITING"
    APPROVED = "APPROVED"
    SCHEDULED = "SCHEDULED"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    ARCHIVED = "ARCHIVED"


class LLMStage(str, enum.Enum):
    PREFILTER = "prefilter"
    CLASSIFY = "classify"
    REWRITE = "rewrite"
    REVISION = "revision"


class LLMCallStatus(str, enum.Enum):
    OK = "ok"
    ERROR = "error"
    PARSE_ERROR = "parse_error"


class DraftOrigin(str, enum.Enum):
    ORIGINAL = "original"          # черновик = оригинал (канал без авторерайта)
    LLM_REWRITE = "llm_rewrite"
    LLM_REVISION = "llm_revision"
    MANUAL = "manual"


class MediaType(str, enum.Enum):
    PHOTO = "photo"
    VIDEO = "video"


class EventActor(str, enum.Enum):
    SYSTEM = "system"
    OWNER = "owner"
    LLM = "llm"


class PublishMode(str, enum.Enum):
    NOW = "now"
    QUEUE = "queue"
    SCHEDULE = "schedule"


class PublishJobState(str, enum.Enum):
    QUEUED = "queued"
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"