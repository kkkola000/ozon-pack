"""Общие зависимости FastAPI: текущий пользователь, CSRF, шаблоны."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from .config import BASE_DIR, settings
from .version import build_label

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

# Выбранный кабинет держим в отдельной куке: менять его может любой вошедший,
# поэтому переподписывать сессию (и сбрасывать CSRF) на каждое переключение незачем.
ACCOUNT_COOKIE = "ozp_account"


def current_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется вход")
    return user


def current_account(request: Request) -> dict | None:
    """Кабинет текущей вкладки: из куки, иначе первый включённый."""
    from . import accounts

    cached = getattr(request.state, "account", None)
    if cached is not None:
        return cached
    account = accounts.resolve(request.cookies.get(ACCOUNT_COOKIE))
    request.state.account = account
    return account


def require_account(request: Request) -> dict:
    account = current_account(request)
    if not account:
        raise HTTPException(status_code=503, detail="Не добавлен ни один кабинет — откройте «Настройки»")
    return account


def require_ozon_account(request: Request) -> dict:
    account = require_account(request)
    if account["marketplace"] != "ozon":
        raise HTTPException(status_code=409, detail="Этот раздел работает только с кабинетами Ozon")
    return account


def require_avito_account(request: Request) -> dict:
    account = require_account(request)
    if account["marketplace"] != "avito":
        raise HTTPException(status_code=409, detail="Этот раздел работает только с кабинетами Avito")
    return account


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


def nav_counters(request: Request) -> dict:
    """Счётчики для шапки текущего кабинета — запросы дешёвые."""
    from . import db

    account = current_account(request)
    if not account:
        return {"packaging": 0, "deliver": 0, "returns": 0, "avito_confirm": 0, "avito_ship": 0,
                "avito_returns": 0}
    account_id = account["id"]

    def count(sql: str, params: tuple = ()) -> int:
        row = db.query_one(sql, params)
        return row["c"] if row else 0

    if account["marketplace"] == "avito":
        return {
            "packaging": 0,
            "deliver": 0,
            "returns": 0,
            "avito_confirm": count(
                "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND status = 'on_confirmation'",
                (account_id,),
            ),
            "avito_ship": count(
                "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND status = 'ready_to_ship'",
                (account_id,),
            ),
            "avito_returns": count(
                "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND status = 'on_return' "
                "AND return_status = 'ready_to_pickup' AND taken_at IS NULL",
                (account_id,),
            ),
        }
    return {
        "packaging": count(
            "SELECT COUNT(*) AS c FROM postings WHERE account_id = ? AND status = 'awaiting_packaging'",
            (account_id,),
        ),
        "deliver": count(
            "SELECT COUNT(*) AS c FROM postings WHERE account_id = ? AND status = 'awaiting_deliver' "
            "AND local_state = 'new'",
            (account_id,),
        ),
        "returns": count(
            "SELECT COUNT(*) AS c FROM returns WHERE account_id = ? AND is_ready = 1 AND taken_at IS NULL",
            (account_id,),
        ),
        "avito_confirm": 0,
        "avito_ship": 0,
        "avito_returns": 0,
    }


def static_version() -> str:
    """Метка версии статики — по времени изменения файлов в app/static.

    Без неё браузер сборщика может месяцами держать закешированный скрипт и не
    увидеть исправление.
    """
    global _static_version
    if _static_version is None:
        static_dir = BASE_DIR / "app" / "static"
        try:
            newest = max(path.stat().st_mtime for path in static_dir.iterdir() if path.is_file())
        except (OSError, ValueError):
            newest = 0
        _static_version = str(int(newest))
    return _static_version


_static_version: str | None = None


def demo_mode(request: Request) -> bool:
    from . import accounts

    return accounts.is_demo(current_account(request))


def account_switcher(request: Request) -> dict:
    """Данные для переключателя кабинетов в шапке."""
    from . import accounts

    return {
        "current": current_account(request),
        "accounts": accounts.all_accounts(active_only=True),
    }


templates.env.globals["build_label"] = build_label
templates.env.globals["nav_counters"] = nav_counters
templates.env.globals["demo_mode"] = demo_mode
templates.env.globals["account_switcher"] = account_switcher
templates.env.globals["static_version"] = static_version


