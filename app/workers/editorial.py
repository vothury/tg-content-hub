"""Виртуальная редакция: цикл журналист → главред → сбор/текст.

Шаг 1: каркас — расписание циклов из настройки editorial_cycle_times,
фазы будут добавлены в Шагах 2-4. Один цикл = один проход всех фаз.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from app.common.logging import setup_logging
from app.db.session import session_scope
from app.services import monitor
from app.services.settings import Keys, get_setting
from app.services.times import owner_now

log = setup_logging("editorial")


async def _settings() -> tuple[int, str]:
    async with session_scope() as session:
        enabled = int(await get_setting(session, Keys.EDITORIAL_ENABLED))
        times = str(await get_setting(session, Keys.EDITORIAL_CYCLE_TIMES))
    return enabled, times


def _next_cycle_at(times: str):
    now = owner_now()
    slots = []
    for part in times.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        h, m = part.split(":")
        t = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        if t > now:
            slots.append(t)
    return min(slots) if slots else (now + timedelta(days=1)).replace(
        hour=0, minute=5, second=0, microsecond=0)


async def run_cycle() -> None:
    from app.services.editorial_journalist import run_journalist_phase
    log.info("редакция: цикл начат")
    await run_journalist_phase()
    # Фаза 2 (Шаг 3): главред — решения hypothesis/rewrite, задания в topics
    # Фаза 3 (Шаг 4): сбор материалов, вердикт, текст статьи в articles
    log.info("редакция: цикл завершён (фаза журналиста отработала)")


async def main() -> None:
    log.info("editorial: worker запущен (виртуальная редакция)")
    while True:
        enabled, times = await _settings()
        if not enabled:
            log.info("editorial: выключен настройкой — пауза 60 мин")
            await asyncio.sleep(3600)
            continue
        nxt = _next_cycle_at(times)
        delay = (nxt - owner_now()).total_seconds()
        log.info("editorial: следующий цикл в %s (через %.0f мин)",
                 nxt.strftime("%d.%m %H:%M"), delay / 60)
        await asyncio.sleep(max(30, delay))
        try:
            await run_cycle()
        except Exception:  # noqa: BLE001
            log.exception("editorial: сбой цикла")
        await monitor.heartbeat("editorial")


if __name__ == "__main__":
    asyncio.run(main())