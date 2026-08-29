import pytest

from app.services.sources_sync import SourcesFileError, load_sources_file


def _write(tmp_path, text: str):
    p = tmp_path / "sources.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_parse_minimal(tmp_path) -> None:
    p = _write(tmp_path, "sources:\n  - username: '@one'\n    kind: test\n  - username: two\n")
    entries = load_sources_file(p)
    assert entries is not None
    assert entries[0]["username"] == "one"
    assert entries[0]["kind"].value == "test"
    assert entries[0]["enabled"] is True
    assert entries[1]["kind"].value == "external"  # дефолт


def test_missing_file_returns_none(tmp_path) -> None:
    assert load_sources_file(tmp_path / "nope.yaml") is None


def test_bad_kind_raises(tmp_path) -> None:
    p = _write(tmp_path, "sources:\n  - username: one\n    kind: weird\n")
    with pytest.raises(SourcesFileError):
        load_sources_file(p)