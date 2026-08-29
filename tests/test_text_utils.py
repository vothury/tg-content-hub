from app.services.text import make_text_hash, normalize_text


def test_normalize_collapses_whitespace_and_lowercases() -> None:
    assert normalize_text("  Привет,\n   МИР!  ") == "привет, мир!"


def test_normalize_empty() -> None:
    assert normalize_text(None) == ""
    assert normalize_text("") == ""


def test_hash_stable_and_none_for_empty() -> None:
    a = make_text_hash(normalize_text("Один и тот же пост"))
    b = make_text_hash(normalize_text("один и тот же   ПОСТ"))
    assert a == b
    assert make_text_hash("") is None