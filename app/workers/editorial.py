"""Виртуальная редакция: цикл журналист → главред → (фаза 3: сбор/текст).

Цикл стартует по расписанию editorial_cycle_times; один цикл = один проход
всех фаз. Фаза 1: журналист собирает заголовки (web + tg). Фаза 2: главред
выбирает темы (topics) и привязывает к ним заголовки. Фаза 3 (следующий
пакет): материалы → вердикт → текст статьи → Post на штатное ревью.
Пустой цикл (журналист не принёс ни одного заголовка) трактуется как сбой
сети/DNS и однократно повторяется через 10 минут.
"""
from __future__ import annotations

import asyncio
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
    """Ближайший слот расписания сегодня; если все прошли — завтра 00:05.

    Битые элементы расписания пропускаются, чтобы ошибка настройки не
    уронила воркер в crash-loop.
    """
    now = owner_now()
    slots = []
    for part in times.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        h, m, *rest = part.split(":")
        if rest:
            continue
        try:
            t = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        except ValueError:
            log.warning("editorial: пропущен битый слот расписания %r", part)
            continue
        if t > now:
            slots.append(t)
    return min(slots) if slots else (now + timedelta(days=1)).replace(
        hour=0, minute=5, second=0, microsecond=0)


async def run_cycle() -> bool:
    """Один проход всех фаз. Возвращает True, если цикл пустой (нужен повтор)."""
    from app.services.editorial_chief import (HEADLINE_RETENTION_DAYS,
                                               prune_headlines, run_chief_phase)
    from app.services.editorial_journalist import run_journalist_phase

    log.info("редакция: цикл начат")
    from app.services.editorial_writer import run_writer_phase
    web_n, tg_n = await run_journalist_phase()
    topics_n = await run_chief_phase()
    articles_n = await run_writer_phase()
    pruned = await prune_headlines(HEADLINE_RETENTION_DAYS)
    log.info("редакция: цикл завершён (заголовков web=%d tg=%d, тем=%d, статей=%d, prune=%d)",
             web_n, tg_n, topics_n, articles_n, pruned)
    return web_n == 0 and tg_n == 0


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
            empty = await run_cycle()
        except Exception:  # noqa: BLE001
            log.exception("editorial: сбой цикла")
            empty = True
        if empty:
            log.warning("editorial: цикл без результата — повтор через 10 минут (сеть/DNS?)")
            await asyncio.sleep(600)
            try:
                await run_cycle()
            except Exception:  # noqa: BLE001
                log.exception("editorial: повтор цикла тоже упал")
        await monitor.heartbeat("editorial")


if __name__ == "__main__":
    asyncio.run(main())