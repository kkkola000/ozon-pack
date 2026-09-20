"""Точка входа: сборка FastAPI-приложения, сессии и маршруты."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import accounts, db, deps, security, sync
from .config import BASE_DIR
from .routes import admin, auth, avito, orders, pack, products, reports, returns, yandex
from .version import get_commit, get_version

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("app")

# Открытые без входа адреса. Сверяем точно, а не по началу строки: при
# startswith любой будущий маршрут вроде /login-sso или /healthz-details
# оказался бы открыт молча, никак этого не обозначив.
PUBLIC_PATHS = frozenset({"/login", "/healthz", "/favicon.ico", "/static"})
# Статика — единственное, где адрес продолжается: /static/app.js и прочие.
PUBLIC_PREFIXES = ("/static/",)


def _warn_about_spoofable_client_ip() -> None:
    """Предупредить, если панель верит X-Forwarded-For от кого угодно.

    Со звёздочкой uvicorn берёт адрес посетителя прямо из заголовка запроса, а
    значит его подделает любой, кто дотянется до порта: IP_ALLOWLIST перестаёт
    ограничивать, счётчик попыток входа обнуляется каждым запросом, а в журнале
    оказываются выдуманные адреса. Указывать нужно адрес своего прокси.
    """
    if os.getenv("FORWARDED_ALLOW_IPS", "").strip() != "*":
        return
    log.warning(
        "FORWARDED_ALLOW_IPS=* — адрес посетителя подделывается заголовком "
        "X-Forwarded-For: IP_ALLOWLIST и защита от подбора пароля не работают. "
        "Укажите адрес обратного прокси (за nginx на том же сервере — 127.0.0.1)."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _warn_about_spoofable_client_ip()
    db.init_db()
    without_keys = [a["title"] for a in accounts.all_accounts(active_only=True) if not accounts.is_configured(a)]
    if without_keys:
        log.warning("Кабинеты без ключей (данные не загружаются): %s", ", ".join(without_keys))
    sync.start_worker()
    yield
    worker = sync.get_worker()
    if worker:
        worker.stop()


class Utf8JSONResponse(JSONResponse):
    """JSON с явной кодировкой.

    Без «charset=utf-8» браузер, открывший адрес напрямую (а не через fetch),
    угадывает кодировку по настройкам системы. На русской Windows это CP1251, и
    сообщение приходит нечитаемым: «РўСЂРµР±СѓРµС‚СЃСЏ РІС…РѕРґ» вместо
    «Требуется вход». Так оператору и показали причину ошибки.
    """

    media_type = "application/json; charset=utf-8"


app = FastAPI(title="Ozon Pack", docs_url=None, redoc_url=None, lifespan=lifespan,
              default_response_class=Utf8JSONResponse)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")


def _wants_html(request: Request) -> bool:
    """Это переход по ссылке, а не запрос из скрипта?

    По ссылкам открываются печать и выгрузки — на них человек смотрит глазами,
    и JSON-ответ ему ничего не объясняет.
    """
    if request.headers.get("X-Requested-With"):
        return False
    accept = request.headers.get("accept", "")
    return "text/html" in accept and "application/json" not in accept.split(",")[0]


@app.exception_handler(StarletteHTTPException)
async def error_response(request: Request, exc: StarletteHTTPException):
    """Ошибку показываем так, как её будут читать.

    Перешли по ссылке — страница с объяснением; запросил скрипт — JSON,
    который разберёт панель.
    """
    detail = exc.detail if isinstance(exc.detail, str) else "Ошибка"
    if _wants_html(request):
        return deps.templates.TemplateResponse(
            request,
            "error.html",
            {
                "request": request,
                "user": getattr(request.state, "user", None),
                "status": exc.status_code,
                "detail": detail,
                "active_tab": "",
            },
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )
    return Utf8JSONResponse({"detail": detail}, status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    client_ip = request.client.host if request.client else None
    if not security.ip_allowed(client_ip):
        return Utf8JSONResponse({"detail": "Доступ с этого IP запрещён"}, status_code=403)

    session = security.read_session(request.cookies.get(security.SESSION_COOKIE))
    request.state.session = session
    request.state.user = security.session_user(session)

    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES) or request.state.user:
        return await call_next(request)

    if path.startswith("/api/"):
        return Utf8JSONResponse({"detail": "Требуется вход"}, status_code=401)
    return RedirectResponse(f"/login?next={path}", status_code=303)


FAVICON = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    b'<text y="26" font-size="26">\xf0\x9f\x93\xa6</text></svg>'
)


@app.get("/favicon.ico")
def favicon():
    return Response(content=FAVICON, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


@app.get("/healthz")
def healthz(request: Request):
    """Проверка живости. Без входа — только сам факт, что панель отвечает.

    Версия, коммит и число кабинетов помогают не столько мониторингу, сколько
    тому, кто ищет в интернете сервер с известной уязвимой сборкой, поэтому
    вошедшим отдаём подробности, а всем остальным — статус.
    """
    if not getattr(request.state, "user", None):
        return {"status": "ok"}
    active = accounts.all_accounts(active_only=True)
    return {
        "status": "ok",
        "accounts": len(active),
        "configured": sum(1 for a in active if accounts.is_configured(a)),
        "version": get_version(),
        "commit": get_commit(),
    }


@app.get("/")
def index(request: Request):
    """Стартовая страница зависит от кабинета: у каждой площадки своя сборка."""
    account = deps.current_account(request)
    if account and account["marketplace"] == "avito":
        # У Avito своё рабочее место сборщика — с него и начинаем, как на Ozon.
        return RedirectResponse("/avito/pack", status_code=303)
    if account and account["marketplace"] == "yandex":
        return RedirectResponse("/yandex/pack", status_code=303)
    return RedirectResponse("/pack", status_code=303)


app.include_router(auth.router)
app.include_router(pack.router)
app.include_router(orders.router)
app.include_router(returns.router)
app.include_router(products.router)
app.include_router(avito.router)
app.include_router(yandex.router)
app.include_router(reports.router)
app.include_router(admin.router)
