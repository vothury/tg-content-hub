def test_metadata_contains_core_tables() -> None:
    """Все обязательные сущности из ТЗ зарегистрированы в metadata."""
    import app.db.models  # noqa: F401

    from app.db.base import Base

    expected = {
        "sources",
        "target_channels",
        "style_profiles",
        "posts",
        "post_draft_versions",
        "media_items",
        "llm_calls",
        "post_events",
        "publish_jobs",
        "app_settings",
    }
    assert expected <= set(Base.metadata.tables)


def test_settings_defaults() -> None:
    """Дефолты предохранителей заданы."""
    from app.config import Settings

    s = Settings(_env_file=None)
    assert s.max_llm_budget_usd_per_day > 0
    assert s.max_candidates_per_day > 0