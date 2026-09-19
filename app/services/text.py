"""Нормализация текста и хеширование для дедупликации."""
from __future__ import annotations

import hashlib
import re
from html import unescape

_WS = re.compile(r"\s+")


def normalize_text(text: str | None) -> str:
    """Свёртка пробельных последовательностей + нижний регистр. Нет текста -> ''."""
    if not text:
        return ""
    return _WS.sub(" ", text).strip().casefold()


def make_text_hash(normalized: str) -> str | None:
    """SHA-256 нормализованного текста.

    Для постов без текста возвращаем None: иначе все фото-посты без подписи
    получили бы одинаковый хеш и ложно считались дублями.
    """
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


_A_TAG_RE = re.compile(r'<a\b[^>]*?href="([^"]*)"[^>]*>(.*?)</a>', re.S | re.I)
_ANY_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(text: str | None) -> str | None:
    """HTML-фрагменты (t.me/s, веб-скрапы) -> читаемый текст; <a> -> markdown [якорь](url)."""
    if not text or "<" not in text:
        return text
    s = _A_TAG_RE.sub(
        lambda m: f"[{_ANY_TAG_RE.sub('', m.group(2)).strip() or m.group(1)}]({unescape(m.group(1))})",
        text)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = _ANY_TAG_RE.sub("", s)
    s = unescape(s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()