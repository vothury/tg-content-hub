"""Нормализация текста и хеширование для дедупликации."""
from __future__ import annotations

import hashlib
import re

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