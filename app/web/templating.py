from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.services.times import owner_tz

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def _dt(v):
    if not v:
        return ""
    if isinstance(v, str):
        return v
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(owner_tz()).strftime("%d.%m %H:%M")


templates.env.filters["dt"] = _dt