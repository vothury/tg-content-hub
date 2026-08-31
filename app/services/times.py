"""Время владельца: хранение в UTC, ввод и отображение в таймзоне владельца."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import settings


def owner_tz():
    try:
        return ZoneInfo(settings.owner_timezone)
    except Exception:  # noqa: BLE001 — некорректный пояс -> UTC
        return timezone.utc


def owner_now() -> datetime:
    return datetime.now(owner_tz())


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=owner_tz())
    return dt.astimezone(timezone.utc)


def fmt_owner(dt_utc: datetime) -> str:
    return dt_utc.astimezone(owner_tz()).strftime("%Y-%m-%d %H:%M")


def parse_scheduled(raw: str) -> datetime | None:
    """Форматы: 'ГГГГ-ММ-ДД ЧЧ:ММ', 'ДД.ММ.ГГГГ ЧЧ:ММ' или 'ЧЧ:ММ' (ближайшее будущее)."""
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"):
        try:
            return to_utc(datetime.strptime(raw, fmt))
        except ValueError:
            continue
    try:
        t = datetime.strptime(raw, "%H:%M").time()
    except ValueError:
        return None
    now = owner_now()
    candidate = datetime.combine(now.date(), t, tzinfo=owner_tz())
    if candidate <= now:
        candidate += timedelta(days=1)
    return to_utc(candidate)