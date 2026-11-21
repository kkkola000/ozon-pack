from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from pack_v2 import __version__
from pack_v2.infrastructure.config import Settings, load_settings
from pack_v2.infrastructure.crypto import SecretBox
from pack_v2.infrastructure.database import Database
from pack_v2.integrations.api import build_router as connections_router
from pack_v2.integrations.contracts import AccessChecker
from pack_v2.integrations.ozon.access import OzonAccessChecker
from pack_v2.integrations.public import Connections
from pack_v2.modules.identity.public import Identity
from pack_v2.shared.errors import Conflict, DomainError, Forbidden, NotFound, RateLimited
from pack_v2.web.body_limit import AuthBodyLimit
from pack_v2.web.routes import build_router
from pack_v2.web.views import render

from .models import load_models

SCHEMA_REVISION = "0002_accounts"


def create_app(settings: Settings | None = None, checker: AccessChecker | None = None) -> FastAPI:
    settings = settings or load_settings()
    load_models()
    db = Database(settings)

    @asynccontextmanager
    async def lifespan(app):
        yield
        db.close()

    app = FastAPI(
        title="Pack — новая архитектура",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings, app.state.db = settings, db
    app.state.identity = Identity(db, settings)
    app.state.connections = Connections(
        db,
        SecretBox(settings.encryption_key.get_secret_value()),
        checker if checker is not None else OzonAccessChecker(),
    )
    app.state.login_signer = URLSafeTimedSerializer(
        settings.secret_key.get_secret_value(), salt="pack-v2-login"
    )
    web = Path(__file__).resolve().parents[1] / "web"
    app.state.templates = Jinja2Templates(directory=web / "templates")
    app.state.templates.env.globals["version"] = __version__
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(AuthBodyLimit)
    app.mount("/static-v2", StaticFiles(directory=web / "static"), name="static-v2")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if not request.url.path.startswith("/static-v2/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def error(request: Request, status: int, message: str, headers=None):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": message}, status_code=status, headers=headers)
        response = render(
            request,
            "error.html",
            saved=getattr(request.state, "current", None),
            title="Не удалось выполнить действие",
            message=message,
        )
        response.status_code = status
        if headers:
            response.headers.update(headers)
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        status = (
            404
            if isinstance(exc, NotFound)
            else 403
            if isinstance(exc, Forbidden)
            else 409
            if isinstance(exc, Conflict)
            else 429
            if isinstance(exc, RateLimited)
            else 400
        )
        headers = {"Retry-After": str(settings.login_window_seconds)} if status == 429 else None
        return error(request, status, str(exc), headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # FastAPI's default errors may echo the whole body, including API keys.
        return error(request, 422, "Проверьте обязательные поля и формат данных")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if exc.status_code == 303:
            return RedirectResponse(exc.headers["Location"], 303)
        return error(request, exc.status_code, str(exc.detail), exc.headers)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        return error(request, 503, "База новой панели недоступна или миграции не применены")

    @app.get("/healthz")
    def health():
        return {"status": "ok", "service": "pack-v2", "version": __version__}

    @app.get("/readyz")
    def readiness():
        try:
            with db.engine.connect() as connection:
                revision = connection.scalar(text("SELECT version_num FROM pack_v2.alembic_version"))
            ready = revision == SCHEMA_REVISION
        except SQLAlchemyError:
            ready = False
        return JSONResponse(
            {"status": "ready" if ready else "not_ready", "service": "pack-v2"},
            status_code=200 if ready else 503,
        )

    app.include_router(build_router())
    app.include_router(connections_router())
    return app
