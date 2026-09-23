import asyncio

from app.config import settings
from app.db.session import session_scope
from app.services.price_watch import (_choose_endpoints, _fetch_endpoints, _label,
                                      _watched_targets)
from app.services.settings import Keys, get_setting


async def main():
    targets = await _watched_targets()
    async with session_scope() as session:
        scope = str(await get_setting(session, Keys.PRICE_WATCH_SCOPE))
        key = str(await get_setting(session, Keys.OPENROUTER_MANAGEMENT_KEY) or "")
    key = key or settings.openrouter_api_key
    print("режим:", scope or "off")
    for m, pinned in sorted(targets.items()):
        eps = await _fetch_endpoints(m, key)
        print(m, "| закреплено:", sorted(pinned) or "—", "| эндпоинтов:", len(eps))
        for e in eps:
            print("   -", _label(e), "| in", round(e["prompt"] * 1e6, 3),
                  "| out", round(e["completion"] * 1e6, 3), "| uptime", e.get("uptime"))
        print("   выбрано:", [_label(c) for c in _choose_endpoints(eps, scope, pinned)])


asyncio.run(main())