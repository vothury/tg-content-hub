import logging
import secrets
import asyncio

from fastapi import FastAPI, Request, Depends
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware
from contextlib import asynccontextmanager

from app.config import settings
from app.db.session import session_scope
from app.redis_client import get_redis
from app.web.auth import AuthRequired, require_auth
from app.web.routers import auth_routes, content_page, dashboard, post_detail, posts, queue_page, settings_page
from app.web.templating import WEB_DIR
from app.services import monitor


log = logging.getLogger("web")


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(monitor.monitor_loop())
    yield
    task.cancel()


app = FastAPI(title="TG Content Hub", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


class _ApiAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "/api/" not in record.getMessage()

logging.getLogger("uvicorn.access").addFilter(_ApiAccessFilter())


SECRET = settings.secret_key or secrets.token_hex(32)
if not settings.secret_key:
    log.warning("SECRET_KEY не задан — сессии будут сбрасываться при каждом рестарте")
if not settings.admin_password:
    log.warning("ADMIN_PASSWORD не задан — вход в админку будет невозможен")

app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET,
    max_age=7 * 24 * 3600,
    same_site="lax",
    https_only=False,
)

app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")


async def _auth_required_handler(request: Request, exc: AuthRequired):
    return RedirectResponse("/login", status_code=303)


app.add_exception_handler(AuthRequired, _auth_required_handler)

app.include_router(auth_routes.router)
app.include_router(dashboard.router)
app.include_router(posts.router)
app.include_router(post_detail.router)
app.include_router(settings_page.router)
app.include_router(content_page.router)
app.include_router(queue_page.router)


@app.get("/healthz")
async def healthz():
    status = {"status": "ok", "environment": getattr(settings, "environment", "dev")}
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
        status["postgres"] = "ok"
    except Exception:  # noqa: BLE001
        status["postgres"] = "error"
        status["status"] = "degraded"
    try:
        await get_redis().ping()
        status["redis"] = "ok"
    except Exception:  # noqa: BLE001
        status["redis"] = "error"
        status["status"] = "degraded"
    return JSONResponse(status)


@app.get("/sw.js")
async def sw():
    return FileResponse(WEB_DIR / "static" / "sw.js", media_type="application/javascript")


@app.get("/api/status", dependencies=[Depends(require_auth)])
async def api_status():
    return JSONResponse(await monitor.get_status())