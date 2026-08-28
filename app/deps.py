"""Общие зависимости FastAPI: текущий пользователь, CSRF, шаблоны."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from .config import BASE_DIR, settings

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


def current_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется вход")
    return user


def require_admin(request: Request) -> dict:
    user = current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Доступно только администратору")
    return user


def check_csrf(request: Request) -> None:
    """Защита от запросов со сторонних сайтов."""
    session = getattr(request.state, "session", None) or {}
    token = request.headers.get("X-CSRF-Token") or ""
    if not token or token != session.get("csrf"):
        raise HTTPException(status_code=403, detail="Недействительный CSRF-токен")


def local_dt(value: str | None, fmt: str = "%d.%m %H:%M") -> str:
    """ISO-8601 UTC ->локальное время склада (TZ_OFFSET_HOURS)."""
    if not value:
        return "—"
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    shifted = moment.astimezone(timezone.utc).timestamp() + settings.timezone_offset * 3600
    return datetime.fromtimestamp(shifted, tz=timezone.utc).strftime(fmt)


templates.env.filters["local_dt"] = local_dt
templates.env.globals["settings"] = settings


def nav_counters() -> dict:
    """Счётчики для шапки — считаются на каждый рендер, запросы дешёвые."""
    from . import db

    def count(sql: str, params: tuple = ()) -> int:
        row = db.query_one(sql, params)
        return row["c"] if row else 0

    return {
        "packaging": count("SELECT COUNT(*) AS c FROM postings WHERE status = 'awaiting_packaging'"),
        "deliver": count(
            "SELECT COUNT(*) AS c FROM postings WHERE status = 'awaiting_deliver' AND local_state = 'new'"
        ),
        "returns": count("SELECT COUNT(*) AS c FROM returns WHERE is_ready = 1 AND taken_at IS NULL"),
    }


def demo_mode() -> bool:
    from .credentials import is_demo

    return is_demo()


templates.env.globals["nav_counters"] = nav_counters
templates.env.globals["demo_mode"] = demo_mode
