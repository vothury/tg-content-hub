"""Веб-админка (каркас). Полный интерфейс — этап 6.

Порт публикуется только на 127.0.0.1 (вариант А): наружу не торчит,
доступ с VPS через Tailscale/SSH-туннель.
"""
from fastapi import FastAPI
from sqlalchemy import text

from app.common.logging import setup_logging
from app.config import settings
from app.db.session import session_scope
from app.redis_client import get_redis

setup_logging("api")

app = FastAPI(title="TG Content Hub — админка", version="0.1.0")


@app.get("/")
async def root() -> dict:
    return {"service": "tg-content-hub api", "stage": 0, "environment": settings.environment}


@app.get("/healthz")
async def healthz() -> dict:
    report: dict = {"status": "ok", "environment": settings.environment}

    try:
        async with session_scope() as session:
            await session.execute(text("select 1"))
        report["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001 — диагностика
        report["postgres"] = f"error: {exc.__class__.__name__}"
        report["status"] = "degraded"

    try:
        await get_redis().ping()
        report["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001 — диагностика
        report["redis"] = f"error: {exc.__class__.__name__}"
        report["status"] = "degraded"

    return report