"""Одноразовая починка битых list/providers-значений в app_settings.

Запуск:
    docker compose run --rm --entrypoint python api scripts/repair_settings.py            # dry-run
    docker compose run --rm --entrypoint python api scripts/repair_settings.py --apply    # починить
"""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.db.models import AppSetting
from app.db.session import session_scope
from app.services.settings import repair_list
from app.web.routers.settings_page import EDITABLE

TYPES = {e["key"]: e["type"] for e in EDITABLE if e["type"] in ("list", "providers")}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="реально записать исправления")
    args = ap.parse_args()
    changed = 0
    async with session_scope() as session:
        rows = (await session.execute(
            select(AppSetting).order_by(AppSetting.key))).scalars().all()
        for row in rows:
            t = TYPES.get(row.key)
            if t is None or row.value is None:
                continue
            if t == "list":
                fixed = repair_list(row.value)
            else:
                if isinstance(row.value, dict):
                    order = repair_list(row.value.get("order") or [])
                else:
                    order = repair_list(row.value)
                fixed = {"order": order, "allow_fallbacks": True} if order else {}
            if fixed != row.value:
                print(f"{row.key}:\n  было:  {row.value!r}\n  стало: {fixed!r}")
                if args.apply:
                    row.value = fixed
                    changed += 1
        if args.apply:
            await session.commit()
    print(("изменено строк: " + str(changed)) if args.apply else "dry-run: добавьте --apply")


asyncio.run(main())