"""Структурированные ответы моделей и устойчивый парсинг JSON."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


class LLMParseError(Exception):
    """Ответ модели не является корректным JSON ожидаемой формы."""


def extract_json(content: str) -> dict:
    """Извлекает JSON-объект из ответа модели: ```json```-обёртки, срез по фигурным
    скобкам, а при отказе — ремонт (голые значения/ключи, «ёлочки», висячие запятые)."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
    candidates = [text]
    start = text.find("{")
    if start != -1:
        depth, end = 0, -1
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end > start:
            candidates.append(text[start:end + 1])   # балансный срез: JSON до первой полной скобки
        end2 = text.rfind("}")
        if end2 > start:
            candidates.append(text[start:end2 + 1])  # прежний срез остаётся запасным кандидатом
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    candidates += [_repair_json_text(c) for c in list(candidates)]
    candidates += [c.replace("'", '"') for c in candidates[1:3]]
    for cand in candidates:
        try:
            data = json.loads(cand)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
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


_JSON_KEY_LINE_RE = re.compile(
    r'^(?:"(?P<qkey>[^"]+)"|(?P<bkey>[A-Za-z_][A-Za-z0-9_]*))\s*:\s*(?P<val>.*?)(?P<comma>,?)$')


def _quote_if_bare(val: str) -> str:
    """Если значение не число/bool/null/уже в кавычках/скобках — обернуть в двойные кавычки."""
    v = val.strip()
    if v == "":
        return '""'
    if v[0] in '"[{':
        return v
    if v in ("true", "false", "null"):
        return v
    if re.match(r'^-?\d+(\.\d+)?([eE][+-]?\d+)?$', v):
        return v
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _merge_broken_strings(lines: list) -> list:
    """Склеивает строки, где строковое значение разорвано переводом строки
    (перевод заменяется на экранированный \\n, чтобы json.loads принял)."""
    out, buf, open_str = [], [], False
    for ln in lines:
        quotes = len(re.findall(r'(?<!\\)"', ln))
        if open_str:
            buf.append(ln)
            if quotes % 2 == 1:
                open_str = False
                out.append("\\n".join(buf))
                buf = []
        else:
            buf.append(ln)
            if quotes % 2 == 1:
                open_str = True
            else:
                out.append(buf[0])
                buf = []
    if buf:
        out.append("\\n".join(buf))
    return out


def _repair_json_text(s: str) -> str:
    """Чинит типовые огрехи слабых моделей: голые значения и ключи без кавычек,
    значения в «ёлочках», висячие запятые; переводы строк внутри строк экранируются."""
    s = re.sub(r"(:\s*)«([^»\n]*)»\s*(,?)\s*$", r'\1"\2"\3', s, flags=re.M)
    out = []
    for ln in _merge_broken_strings(s.split("\n")):
        t = ln.strip()
        m = _JSON_KEY_LINE_RE.match(t)
        if m and not t.endswith(("{", "[")):
            key = m.group("qkey") or m.group("bkey")
            out.append(f'"{key}": {_quote_if_bare(m.group("val"))}{m.group("comma")}')
        else:
            out.append(t)
    fixed = "\n".join(out)
    return re.sub(r",(\s*[}\]])", r"\1", fixed)   # висячие запятые перед } / ]


_SAFETY_REPLY_RE = re.compile(r"^\s*(user\s*)?safety\s*[:\-]", re.I)


def is_provider_safety_reply(content: str) -> bool:
    """OpenRouter на free-пуле иногда роутит запрос на модель модерации
    (Nemotron Content Safety), которая отвечает 'User Safety: safe' вместо JSON.
    Это технический сбой маршрутизации, а не ответ по существу."""
    s = (content or "").strip()
    if not s:
        return False
    if _SAFETY_REPLY_RE.match(s):
        return True
    return len(s) <= 40 and s.lower().strip(".!") in (
        "safe", "unsafe", "user safety", "content safe")


def _loads_lenient(content: str):
    """json.loads с починкой частых огрехов модели (через _repair_json_text)."""
    s = _strip_code_fence(content)
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        pass
    l, r = s.find("{"), s.rfind("}")
    sliced = s[l:r + 1] if l != -1 and r > l else s
    for cand in (sliced, _repair_json_text(sliced), _repair_json_text(s),
                 _repair_json_text(sliced).replace("'", '"')):
        try:
            return json.loads(cand)
        except Exception:  # noqa: BLE001
            continue
    raise LLMParseError(f"не удалось извлечь JSON из ответа: {content[:300]!r}")


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


_TASTE_LINE_RE = re.compile(r"(\d+)\s*[:.)\-]?\s*(\d+(?:[.,]\d+)?)")
_AUDIT_LINE_RE = re.compile(r"(\d+)\s*[:.)\-]?\s*\b(profile|secondary|water|ads|other)\b", re.I)


@dataclass
class BacklogScanResult:
    """taste: строка «i score» по всем номерам + JSON-подписи только для keep-ов."""
    items: list = field(default_factory=list)

    @classmethod
    def from_response(cls, content: str) -> "BacklogScanResult":
        s = (content or "").strip()
        caps: dict = {}
        try:
            data = extract_json(s)
            raw = data.get("items") if isinstance(data, dict) else data
            for x in (raw if isinstance(raw, list) else []):
                if not isinstance(x, dict):
                    continue
                try:
                    i = int(x.get("i"))
                except (TypeError, ValueError):
                    continue
                try:
                    sc = float(x.get("score") or 0)
                except (TypeError, ValueError):
                    sc = 0.0
                caps[i] = {"score": max(0.0, min(10.0, sc)),
                           "caption": str(x.get("caption") or "").strip(),
                           "keep": bool(x.get("keep", True))}
        except Exception:  # noqa: BLE001 — JSON может отсутствовать (новый формат)
            pass
        scores: dict = {}
        for m in _TASTE_LINE_RE.finditer(s):
            scores[int(m.group(1))] = float(m.group(2).replace(",", "."))
        items: list = []
        for i, score in sorted(scores.items()):
            j = caps.get(i)
            items.append({"i": i,
                          "keep": True if j is None else j["keep"],
                          "score": max(score, j["score"]) if j else score,
                          "caption": j["caption"] if j else ""})
        if items:
            return cls(items=items)
        if caps:  # запасной путь: модель ответила старым полным JSON без строки
            return cls(items=[{"i": i, "keep": v["keep"], "score": v["score"],
                               "caption": v["caption"]} for i, v in sorted(caps.items())])
        raise LLMParseError(f"нет строки «i score» и нет JSON: {s[:200]!r}")


@dataclass
class BacklogAuditResult:
    """audit: ответ = одна строка «i cat.» на весь батч, JSON не требуется."""
    items: list = field(default_factory=list)

    @classmethod
    def from_response(cls, content: str) -> "BacklogAuditResult":
        s = (content or "").strip()
        items: dict = {}
        for m in _AUDIT_LINE_RE.finditer(s):
            items[int(m.group(1))] = m.group(2).lower()
        if items:
            return cls(items=[{"i": i, "cat": items[i]} for i in sorted(items)])
        alt: list = []
        try:
            data = extract_json(s)
            raw = data.get("items") if isinstance(data, dict) else data
            for x in (raw if isinstance(raw, list) else []):
                if not isinstance(x, dict):
                    continue
                try:
                    i = int(x.get("i"))
                except (TypeError, ValueError):
                    continue
                alt.append({"i": i, "cat": str(x.get("cat") or "other").strip().lower()})
        except Exception:  # noqa: BLE001
            pass
        if alt:
            return cls(items=alt)
        raise LLMParseError(f"нет строки «i cat» и нет JSON: {s[:200]!r}")


_FACTS_R_RE = re.compile(r"(?m)^\s*R\s*:\s*([0-9,\s;\-]+?)\s*$")
_FACTS_LINE_RE = re.compile(r"(?m)^\s*(\d+)\s*\|([^|\n]*)\|([^\n]*)$")


@dataclass
class BacklogFactsResult:
    """facts v2: «R: 2,8,9» + линии «i|obj|fact; fact»; JSON собирается здесь.

    Устойчива к обрезанию: полные линии спасаются; если R объявил номера,
    для которых линии не доехали, — ошибка (батч уйдёт в ретрай).
    Дубликаты номера сливаются (факты объединяются, до 6 шт.).
    Пустой батч («R: -») легален. Запасной путь — прежний JSON-формат.
    """
    items: list = field(default_factory=list)

    @classmethod
    def from_response(cls, content: str) -> "BacklogFactsResult":
        s = (content or "").strip()
        rm = _FACTS_R_RE.search(s)
        expected = {int(t) for t in re.findall(r"\d+", rm.group(1))} if rm else set()
        rows: dict = {}
        for m in _FACTS_LINE_RE.finditer(s):
            i = int(m.group(1))
            obj = m.group(2).strip()
            facts = [f.strip() for f in m.group(3).split(";") if f.strip()]
            if not facts:
                continue
            if i in rows:
                base = rows[i]
                for f in facts:
                    tagged = f if (not obj or obj == base["obj"]) else f"{obj}: {f}"
                    if tagged not in base["facts"] and len(base["facts"]) < 8:
                        base["facts"].append(tagged)
            else:
                rows[i] = {"i": i, "rel": True, "obj": obj, "facts": facts[:8]}
        if rm or rows:
            missing = expected - set(rows)
            if missing:
                raise LLMParseError(
                    f"ответ обрезан: R объявил {sorted(expected)}, нет линий для {sorted(missing)}")
            return cls(items=[rows[k] for k in sorted(rows)])
        items = []
        try:  # запасной путь: старый JSON-формат
            data = extract_json(s)
            raw = data.get("items") if isinstance(data, dict) else data
            for x in (raw if isinstance(raw, list) else []):
                if not isinstance(x, dict):
                    continue
                try:
                    i = int(x.get("i"))
                except (TypeError, ValueError):
                    continue
                facts = x.get("facts") or []
                if not isinstance(facts, list):
                    facts = [str(facts)]
                items.append({"i": i, "rel": bool(x.get("rel")),
                              "obj": str(x.get("obj") or "").strip(),
                              "facts": [str(f).strip() for f in facts if str(f).strip()][:6]})
        except Exception:  # noqa: BLE001
            pass
        if items or '"items"' in s:
            return cls(items=items)
        raise LLMParseError(f"нет ни «R:»/линий, ни JSON: {s[:200]!r}")


@dataclass
class ChiefTopicsResult:
    """Главред: 0..N тем; пустой список = «сегодня нечего производить» — это норма."""
    topics: list = field(default_factory=list)

    @classmethod
    def from_response(cls, content: str) -> "ChiefTopicsResult":
        data = extract_json(content)
        raw = data.get("topics") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            raise LLMParseError("ожидался список topics")
        topics = []
        for x in raw:
            if not isinstance(x, dict):
                continue
            kind = str(x.get("kind") or "").strip().lower()
            if kind not in ("hypothesis", "rewrite"):
                continue
            theme = str(x.get("theme") or "").strip()
            if not theme:
                continue
            nums = []
            for n in x.get("headlines") or []:
                try:
                    nums.append(int(n))
                except (TypeError, ValueError):
                    continue
            topics.append({"kind": kind, "theme": theme,
                           "hypothesis": str(x.get("hypothesis") or "").strip(),
                           "headlines": nums})
        return cls(topics=topics)