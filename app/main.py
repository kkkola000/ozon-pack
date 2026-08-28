"""Точка входа: сборка FastAPI-приложения, сессии и маршруты."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db, security, sync
from .credentials import is_demo
from .config import BASE_DIR, settings
from .routes import admin, auth, orders, pack, returns

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("app")

PUBLIC_PATHS = ("/login", "/static", "/healthz", "/favicon.ico")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    if is_demo():
        log.warning("Демо-режим: данные сгенерированы локально, Ozon не опрашивается")
    sync.start_worker()
    yield
    worker = sync.get_worker()
    if worker:
        worker.stop()


app = FastAPI(title="Ozon Pack", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    client_ip = request.client.host if request.client else None
    if not security.ip_allowed(client_ip):
        return JSONResponse({"detail": "Доступ с этого IP запрещён"}, status_code=403)

    session = security.read_session(request.cookies.get(security.SESSION_COOKIE))
    request.state.session = session
    request.state.user = security.get_user(session["uid"]) if session else None

    path = request.url.path
    if path.startswith(PUBLIC_PATHS) or request.state.user:
        return await call_next(request)

    if path.startswith("/api/"):
        return JSONResponse({"detail": "Требуется вход"}, status_code=401)
    return RedirectResponse(f"/login?next={path}", status_code=303)


FAVICON = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    b'<text y="26" font-size="26">\xf0\x9f\x93\xa6</text></svg>'
)


@app.get("/favicon.ico")
def favicon():
    return Response(content=FAVICON, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


@app.get("/healthz")
def healthz():
    return {"status": "ok", "demo": is_demo()}


@app.get("/")
def index():
    return RedirectResponse("/pack", status_code=303)


app.include_router(auth.router)
app.include_router(pack.router)
app.include_router(orders.router)
app.include_router(returns.router)
app.include_router(admin.router)
