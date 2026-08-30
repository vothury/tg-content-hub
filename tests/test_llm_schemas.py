import pytest

from app.services.llm.schemas import ClassifyResult, LLMParseError, RewriteResult, extract_json


def test_extract_plain_json() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_in_code_fence() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_with_surrounding_text() -> None:
    assert extract_json('Вот результат: {"a": 1} Готово') == {"a": 1}


def test_extract_garbage_raises() -> None:
    with pytest.raises(LLMParseError):
        extract_json("здесь нет json")


def test_classify_parse_and_clamp() -> None:
    r = ClassifyResult.from_response('{"suitable": true, "score": 42, "reason": "ок", "risks": "один"}')
    assert r.suitable is True
    assert r.score == 10.0
    assert r.risks == ["один"]


def test_classify_missing_suitable_raises() -> None:
    with pytest.raises(LLMParseError):
        ClassifyResult.from_response('{"score": 5}')


def test_rewrite_ok() -> None:
    r = RewriteResult.from_response('{"draft": "текст", "warnings": ["осторожно"]}')
    assert r.draft == "текст"
    assert r.warnings == ["осторожно"]


def test_rewrite_empty_draft_raises() -> None:
    with pytest.raises(LLMParseError):
        RewriteResult.from_response('{"draft": "   "}')