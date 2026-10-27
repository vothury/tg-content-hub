"""Отчёт стоимости по дням/стадиям/моделям из llm_calls (до монитора B2).

Показывает, куда уходят деньги конвейера и редакции: дневные итоги,
разрез стадий и моделей; ротации видны как превышение вызовов над постами.

  docker compose run --rm --entrypoint python reader scripts/cost_report.py --days 4
  docker compose run --rm --entrypoint python reader scripts/cost_report.py --days 1 --stage editorial_write
  docker compose run --rm --entrypoint python reader scripts/cost_report.py --days 1 --stage editorial_media
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta

from sqlalchemy import func, select

from app.db.models import LLMCall
from app.db.session import session_scope
from app.services.times import owner_now


async def report(days: int, stage: str) -> None:
    since = owner_now() - timedelta(days=days)
    d = func.date(LLMCall.created_at)
    cost = func.coalesce(func.sum(LLMCall.cost_usd), 0)
    q = (select(d.label("day"), LLMCall.stage, LLMCall.model,
                func.count().label("n"), cost.label("cost"))
         .where(LLMCall.created_at >= since))
    if stage:
        q = q.where(LLMCall.stage == stage)
    q = (q.group_by(d, LLMCall.stage, LLMCall.model)
          .order_by(d.desc(), cost.desc()))
    async with session_scope() as session:
        rows = (await session.execute(q)).all()
    cur_day, day_total = None, 0.0
    for r in rows:
        if str(r.day) != cur_day:
            if cur_day is not None:
                print(f"  итого за {cur_day}: ${day_total:.4f}")
            cur_day, day_total = str(r.day), 0.0
        day_total += float(r.cost)
        print(f"{r.day} | {r.stage:<20} | {r.model:<34} | {r.n:>3} | ${float(r.cost):.4f}")
    if cur_day is not None:
        print(f"  итого за {cur_day}: ${day_total:.4f}")
    if not rows:
        print("вызовов за период нет")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=4)
    ap.add_argument("--stage", default="",
                    help="фильтр по стадии: editorial_write, editorial_gate, editorial_media, classify...")
    args = ap.parse_args()
    asyncio.run(report(args.days, args.stage))


if __name__ == "__main__":
    main()