"""Вход и выход."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db, security
from ..config import settings
from ..credentials import is_demo
from ..deps import templates

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/pack", error: str | None = None):
    if getattr(request.state, "user", None):
        return RedirectResponse(next or "/pack", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"request": request, "next": next, "error": error, "demo": is_demo()}
    )


@router.post("/login")
def login(request: Request, login: str = Form(...), password: str = Form(...), next: str = Form("/pack")):
    client_ip = request.client.host if request.client else "?"
    if security.too_many_attempts(client_ip):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "next": next, "error": "Слишком много попыток. Подождите 5 минут.", "demo": is_demo()},
            status_code=429,
        )

    row = db.query_one("SELECT * FROM users WHERE login = ? AND active = 1", (login.strip(),))
    if not row or not security.verify_password(password, row["password_hash"]):
        security.register_attempt(client_ip)
        db.log_event("login_failed", level="warn", user={"login": login.strip()}, message=f"IP {client_ip}")
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "next": next, "error": "Неверный логин или пароль", "demo": is_demo()},
            status_code=401,
        )

    security.clear_attempts(client_ip)
    token = security.make_session(row["id"], row["login"], row["role"])
    db.log_event("login", user={"id": row["id"], "login": row["login"]}, message=f"IP {client_ip}")
    target = next if next.startswith("/") else "/pack"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        security.SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        # За nginx с TLS (uvicorn --proxy-headers) схема приходит https — тогда
        # помечаем куку Secure, чтобы она не ушла по открытому каналу.
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )
    return response


@router.get("/logout")
def logout(request: Request):
    user = getattr(request.state, "user", None)
    if user:
        db.log_event("logout", user=user)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(security.SESSION_COOKIE, path="/")
    return response
