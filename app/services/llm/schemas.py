"""Структурированные ответы моделей и устойчивый парсинг JSON."""
from __future__ import annotations

import json
import re
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


def _strip_code_fence(content: str) -> str:
    """Убирает markdown-обёртку ```json ... ``` вокруг ответа модели."""
    s = (content or "").strip()
    if s.startswith("```"):
        s = s[3:]
        if s.startswith("json"):
            s = s[4:]
        elif s.startswith("JSON"):
            s = s[4:]
        s = s.strip()
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def _loads_lenient(content: str):
    """json.loads с починкой частых огрехов модели:
    значения в «ёлочках», trailing commas, одинарные кавычки, текст вокруг JSON."""
    s = _strip_code_fence(content)
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        pass
    fixed = re.sub(r"(:\s*)«([^»\n]*)»", r'\1"\2"', s)
    fixed = re.sub(r",(\s*[}\]])", r"\1", fixed)
    fixed = fixed.replace("'", '"')
    l, r = fixed.find("{"), fixed.rfind("}")
    if l != -1 and r > l:
        fixed = fixed[l : r + 1]
    return json.loads(fixed)


@dataclass
class ClassifyResult:
    suitable: bool
    score: float
    reason: str
    risks: list[str] = field(default_factory=list)
    category: str = ""
    canonical: str = ""

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
            canonical=str(data.get("canonical", "")).strip(),
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


@dataclass
class DoubleCheckResult:
    approve: bool
    note: str = ""

    @classmethod
    def from_response(cls, content: str) -> "DoubleCheckResult":
        data = extract_json(content)
        return cls(approve=bool(data.get("approve", False)),
                   note=str(data.get("note", "")).strip())


@dataclass
class DedupConfirmResult:
    same: bool = False

    @classmethod
    def from_response(cls, content: str) -> "DedupConfirmResult":
        data = extract_json(content)
        if not isinstance(data, dict):
            raise LLMParseError(f"ожидался JSON-объект: {str(data)[:300]!r}")
        return cls(same=bool(data.get("same", False)))


@dataclass
class HeadlineListResult:
    items: list = field(default_factory=list)

    @classmethod
    def from_response(cls, content: str) -> "HeadlineListResult":
        data = _loads_lenient(content)
        raw = data.get("headlines") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            raise LLMParseError("ожидался список заголовков")
        items = []
        for x in raw:
            if isinstance(x, dict) and str(x.get("title") or "").strip():
                items.append({"title": str(x["title"]).strip(),
                              "url": str(x.get("url") or "").strip()})
        if not items:
            raise LLMParseError("пустой список заголовков")
        return cls(items=items)


@dataclass
class HeadlineTitleResult:
    title: str = ""

    @classmethod
    def from_response(cls, content: str) -> "HeadlineTitleResult":
        data = _loads_lenient(content)
        title = str((data or {}).get("title") or "").strip()
        if not title:
            raise LLMParseError("пустой заголовок")
        return cls(title=title)


@dataclass
class HeadlinePickResult:
    items: list = field(default_factory=list)

    @classmethod
    def from_response(cls, content: str) -> "HeadlinePickResult":
        data = _loads_lenient(content)
        raw = data.get("items") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            raise LLMParseError("ожидался список items")
        items = []
        for x in raw:
            if isinstance(x, dict):
                try:
                    i = int(x.get("i"))
                except (TypeError, ValueError):
                    continue
                items.append({"i": i, "title": str(x.get("title") or "").strip()})
        return cls(items=items)


@dataclass
class CleanPlanResult:
    remove: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @classmethod
    def from_response(cls, content: str) -> "CleanPlanResult":
        data = _loads_lenient(content)
        raw = data.get("remove") if isinstance(data, dict) else data
        items = []
        for x in (raw or []):
            if isinstance(x, dict):
                txt = str(x.get("text") or "").strip()
                try:
                    i = int(x.get("i"))
                except (TypeError, ValueError):
                    i = None
                items.append({"i": i, "text": txt})
            elif isinstance(x, (int, float)):
                items.append({"i": int(x), "text": ""})  # только номер -> не проверяемо
        warns = [str(w) for w in (data.get("warnings") or [])] if isinstance(data, dict) else []
        return cls(remove=items, warnings=warns)