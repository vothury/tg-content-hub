"""Web Push (VAPID): пуши в PWA даже при закрытом приложении."""
from __future__ import annotations

import asyncio
import base64
import json
import logging

from cryptography.hazmat.primitives.asymmetric import ec

from app.db.models import AppSetting
from app.db.session import session_scope

log = logging.getLogger("webpush")

K_PRIV = "webpush.private_key"
K_PUB = "webpush.public_key"
K_SUBS = "webpush.subscriptions"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


async def get_public_key() -> str:
    async with session_scope() as session:
        priv = await session.get(AppSetting, K_PRIV)
        pub = await session.get(AppSetting, K_PUB)
        if priv is None or pub is None:
            key = ec.generate_private_key(ec.SECP256R1())
            nums = key.private_numbers()
            priv_b = nums.private_value.to_bytes(32, "big")
            pub_b = b"\x04" + nums.public_numbers.x.to_bytes(32, "big") + nums.public_numbers.y.to_bytes(32, "big")
            session.add(AppSetting(key=K_PRIV, value=_b64url(priv_b)))
            session.add(AppSetting(key=K_PUB, value=_b64url(pub_b)))
            await session.commit()
            return _b64url(pub_b)
        return pub.value


async def add_subscription(sub: dict) -> None:
    async with session_scope() as session:
        row = await session.get(AppSetting, K_SUBS)
        subs = row.value if row is not None and isinstance(row.value, list) else []
        if sub not in subs:
            subs.append(sub)
        if row is None:
            session.add(AppSetting(key=K_SUBS, value=subs))
        else:
            row.value = subs
        await session.commit()


async def notify_all(title: str, body: str) -> None:
    from pywebpush import WebPushException, webpush
    async with session_scope() as session:
        priv = await session.get(AppSetting, K_PRIV)
        row = await session.get(AppSetting, K_SUBS)
    if priv is None or row is None or not row.value:
        return
    subs = list(row.value)
    keep = []
    for sub in subs:
        try:
            await asyncio.to_thread(
                webpush,
                subscription_info=sub,
                data=json.dumps({"title": title, "body": body}),
                vapid_private_key=priv.value,
                vapid_claims={"sub": "mailto:admin@local"},
            )
            keep.append(sub)
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                log.info("webpush: подписка недействительна — удалена")
                continue
            keep.append(sub)
        except Exception:  # noqa: BLE001
            log.exception("webpush: сбой отправки")
            keep.append(sub)
    if len(keep) != len(subs):
        async with session_scope() as session:
            r2 = await session.get(AppSetting, K_SUBS)
            r2.value = keep
            await session.commit()