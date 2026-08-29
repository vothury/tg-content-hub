from app.db.enums import PostStatus
from app.services.prefilter import decide


def test_long_text_passes() -> None:
    d = decide("а" * 250, has_media=False, min_text_len=200, blacklist_words=[])
    assert d.status is PostStatus.PREFILTERED
    assert d.action == "prefilter_passed"


def test_short_text_without_media_rejected() -> None:
    d = decide("коротко", has_media=False, min_text_len=200, blacklist_words=[])
    assert d.status is PostStatus.UNSUITABLE
    assert d.reason == "too_short_or_empty"


def test_short_text_with_media_goes_to_visual_review() -> None:
    d = decide("🙂", has_media=True, min_text_len=200, blacklist_words=[])
    assert d.status is PostStatus.NEEDS_MEDIA_REVIEW
    assert d.reason == "short_text_with_media"


def test_blacklist_rejects_case_insensitive() -> None:
    d = decide("здесь есть КАзино и веселье", has_media=True, min_text_len=5, blacklist_words=["казино"])
    assert d.status is PostStatus.UNSUITABLE
    assert d.reason == "blacklist"


def test_empty_blacklist_words_ignored() -> None:
    d = decide("нормальный текст " + "а" * 200, has_media=False, min_text_len=200, blacklist_words=["", "   "])
    assert d.status is PostStatus.PREFILTERED