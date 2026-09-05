"""Вход и выход."""
from __future__ import annotations

from fastapi import APIRouter, Body, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import accounts, db, security
from ..config import settings
from ..deps import ACCOUNT_COOKIE, check_csrf, current_user, templates

router = APIRouter()


def _demo() -> bool:
    """Бейдж «ДЕМО» на странице входа: до входа кабинет ещё не выбран."""
    return accounts.is_demo(accounts.default_account())


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/pack", error: str | None = None):
    if getattr(request.state, "user", None):
        return RedirectResponse(next or "/pack", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"request": request, "next": next, "error": error, "demo": _demo()}
    )


@router.post("/login")
def login(request: Request, login: str = Form(...), password: str = Form(...), next: str = Form("/pack")):
    client_ip = request.client.host if request.client else "?"
    if security.too_many_attempts(client_ip):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "next": next, "error": "Слишком много попыток. Подождите 5 минут.", "demo": _demo()},
            status_code=429,
        )

    row = db.query_one("SELECT * FROM users WHERE login = ? AND active = 1", (login.strip(),))
    if not row or not security.verify_password(password, row["password_hash"]):
        security.register_attempt(client_ip)
        db.log_event("login_failed", level="warn", user={"login": login.strip()}, message=f"IP {client_ip}")
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "next": next, "error": "Неверный логин или пароль", "demo": _demo()},
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


@router.post("/api/account/switch")
def switch_account(request: Request, payload: dict = Body(...)):
    """Переключить кабинет. Данные разделены по account_id, поэтому меняется всё сразу."""
    check_csrf(request)
    user = current_user(request)
    account = accounts.get(payload.get("account_id"))
    if not account or not account["active"]:
        raise HTTPException(status_code=404, detail="Кабинет не найден или выключен")

    db.log_event(
        "account_switch",
        account_id=account["id"],
        user=user,
        message=f"Переключение на кабинет «{account['title']}»",
    )
    # Сборка идёт в конкретном кабинете: чужой раздел после переключения открывать незачем.
    target = str(payload.get("next") or "/")
    if not target.startswith("/"):
        target = "/"
    if account["marketplace"] == "avito" and not target.startswith(("/avito", "/logs", "/settings")):
        target = "/avito"
    if account["marketplace"] == "ozon" and target.startswith("/avito"):
        target = "/pack"

    response = JSONResponse({"status": "ok", "redirect": target, "account": account["title"]})
    response.set_cookie(
        ACCOUNT_COOKIE,
        str(account["id"]),
        max_age=365 * 24 * 3600,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )
    return response
