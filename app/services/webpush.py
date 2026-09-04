"""Web Push (VAPID): пуши в PWA даже при закрытом приложении."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.db.models import AppSetting
from app.db.session import session_scope

log = logging.getLogger("webpush")

K_PRIV = "webpush.private_pem"
K_PUB = "webpush.public_key"
K_SUBS = "webpush.subscriptions"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


async def _set(session, key: str, value) -> None:
    row = await session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value


async def get_public_key() -> str:
    async with session_scope() as session:
        priv = await session.get(AppSetting, K_PRIV)
        pub = await session.get(AppSetting, K_PUB)
        if priv is not None and pub is not None:
            return pub.value
        key = ec.generate_private_key(ec.SECP256R1())
        nums = key.private_numbers()
        pub_b = b"\x04" + nums.public_numbers.x.to_bytes(32, "big") + nums.public_numbers.y.to_bytes(32, "big")
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        await _set(session, K_PRIV, pem)
        await _set(session, K_PUB, _b64url(pub_b))
        await session.commit()
        return _b64url(pub_b)


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
    from py_vapid import Vapid
    from pywebpush import WebPushException, webpush
    async with session_scope() as session:
        priv = await session.get(AppSetting, K_PRIV)
        row = await session.get(AppSetting, K_SUBS)
    if priv is None:
        log.warning("webpush: VAPID-ключ не создан — пуш пропущен")
        return
    if row is None or not row.value:
        log.info("webpush: нет ни одной подписки — пуш пропущен")
        return
    try:
        vapid = Vapid.from_string(priv.value)
        headers = vapid.sign({"sub": "mailto:admin@local", "exp": int(time.time()) + 86400})
    except Exception:
        log.exception("webpush: не удалось подписать VAPID")
        return
    subs = list(row.value)
    keep = []
    sent = 0
    for sub in subs:
        try:
            await asyncio.to_thread(
                webpush,
                sub,
                json.dumps({"title": title, "body": body}),
                headers=headers,
            )
            sent += 1
            keep.append(sub)
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            log.warning("webpush: ошибка отправки (status=%s): %s", status, exc)
            if status in (404, 410):
                continue
            keep.append(sub)
        except Exception:  # noqa: BLE001
            log.exception("webpush: сбой отправки")
            keep.append(sub)
    log.info("webpush: отправлено %d из %d подписок", sent, len(subs))
    if len(keep) != len(subs):
        async with session_scope() as session:
            r2 = await session.get(AppSetting, K_SUBS)
            r2.value = keep
            await session.commit()