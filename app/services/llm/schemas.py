"""Структурированные ответы моделей и устойчивый парсинг JSON."""
from __future__ import annotations

import json
from dataclasses import dataclass, field


class LLMParseError(Exception):
    """Ответ модели не является корректным JSON ожидаемой формы."""


def extract_json(content: str) -> dict:
    """Извлекает JSON-объект из ответа модели, включая обёртки вида ```json."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    raise LLMParseError(f"не удалось извлечь JSON из ответа: {content[:300]!r}")


@dataclass
class ClassifyResult:
    suitable: bool
    score: float
    reason: str
    risks: list[str] = field(default_factory=list)

    @classmethod
    def from_response(cls, content: str) -> "ClassifyResult":
        data = extract_json(content)
        if "suitable" not in data:
            raise LLMParseError(f"нет поля 'suitable': {str(data)[:300]!r}")
        risks = data.get("risks") or []
        if not isinstance(risks, list):
            risks = [str(risks)]
        try:
            score = float(data.get("score", 0))
        except (TypeError, ValueError):
            score = 0.0
        return cls(
            suitable=bool(data["suitable"]),
            score=max(0.0, min(10.0, score)),
            reason=str(data.get("reason", "")).strip(),
            risks=[str(r) for r in risks][:10],
        )


@dataclass
class RewriteResult:
    draft: str
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_response(cls, content: str) -> "RewriteResult":
        data = extract_json(content)
        draft = str(data.get("draft", "")).strip()
        if not draft:
            raise LLMParseError(f"пустое поле 'draft': {str(data)[:300]!r}")
        warnings = data.get("warnings") or []
        if not isinstance(warnings, list):
            warnings = [str(warnings)]
        return cls(draft=draft, warnings=[str(w) for w in warnings][:10])