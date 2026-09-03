"""Монитор здоровья сервисов. Воркеры шлют heartbeat в Redis; веб раз в
15 минут собирает статус (БД, Redis, диск, heartbeat'и) и отдаёт его в бейдж."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import text

from app.db.session import session_scope
from app.redis_client import get_redis

log = logging.getLogger("monitor")

WORKERS = ("reader", "pipeline", "scheduler", "bot")
HB_TTL = 300          # сек жизни heartbeat
CHECK_INTERVAL = 900  # 15 минут
STATUS_KEY = "monitor:status"


async def heartbeat(name: str) -> None:
    try:
        await get_redis().set(f"hb:{name}", datetime.now(timezone.utc).isoformat(), ex=HB_TTL)
    except Exception:  # noqa: BLE001
        pass


async def run_checks() -> dict:
    problems = []
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        problems.append(f"БД недоступна ({exc.__class__.__name__})")
    try:
        await get_redis().ping()
    except Exception as exc:  # noqa: BLE001
        problems.append(f"Redis недоступен ({exc.__class__.__name__})")
    try:
        st = os.statvfs("/")
        free_gb = st.f_bavail * st.f_frsize / (1024 ** 3)
        if free_gb < 1.0:
            problems.append(f"Мало места на диске: {free_gb:.1f} Гб")
    except Exception:  # noqa: BLE001
        pass
    redis = get_redis()
    now = datetime.now(timezone.utc)
    for name in WORKERS:
        try:
            raw = await redis.get(f"hb:{name}")
        except Exception:  # noqa: BLE001
            problems.append(f"{name}: нет связи с Redis")
            continue
        if raw is None:
            problems.append(f"{name}: не запущен или не шлёт heartbeat")
            continue
        age = (now - datetime.fromisoformat(raw)).total_seconds()
        if age > HB_TTL:
            problems.append(f"{name}: не отвечает {int(age // 60)} мин")
    return {"ok": not problems, "problems": problems, "checked_at": now.isoformat()}


async def refresh() -> dict:
    status = await run_checks()
    try:
        await get_redis().set(STATUS_KEY, json.dumps(status, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass
    return status


async def get_status() -> dict:
    try:
        raw = await get_redis().get(STATUS_KEY)
    except Exception:  # noqa: BLE001
        return await refresh()
    if raw is None:
        return await refresh()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return await refresh()


async def monitor_loop() -> None:
    while True:
        try:
            await refresh()
        except Exception:  # noqa: BLE001
            log.exception("monitor: сбой проверки")
        await asyncio.sleep(CHECK_INTERVAL)