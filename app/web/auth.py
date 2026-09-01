"""Аутентификация и CSRF для веб-админки."""
from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request

from app.config import settings


class AuthRequired(Exception):
    """Поднимается, когда нужен вход; обрабатывается редиректом на /login."""


async def require_auth(request: Request) -> None:
    if not request.session.get("authenticated"):
        raise AuthRequired()


def check_password(candidate: str) -> bool:
    expected = settings.admin_password
    if not expected:
        return False
    return hmac.compare_digest(candidate, expected)


def get_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_hex(32)
        request.session["csrf_token"] = token
    return token


async def csrf_protect(request: Request) -> None:
    form = await request.form()
    token = request.session.get("csrf_token")
    submitted = form.get("csrf_token")
    if not token or not submitted or not hmac.compare_digest(token, submitted):
        raise HTTPException(status_code=403, detail="CSRF check failed")