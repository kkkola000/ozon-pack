import hmac
import secrets
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired
from pydantic import BaseModel, Field, SecretStr

from pack_v2.bootstrap.registry import MODULES
from pack_v2.web.deps import COOKIE, LOGIN_CSRF, check_csrf, current
from pack_v2.web.views import NAV, REPORTS, render, settings_page


def safe_next(value: str) -> str:
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return "/"
    return value


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/login")
    def login_page(request: Request, next: str = "/"):
        if request.app.state.identity.current(request.cookies.get(COOKIE)):
            return RedirectResponse(safe_next(next), 303)
        nonce = request.app.state.login_signer.dumps(secrets.token_urlsafe(32))
        response = render(request, "login.html", login_csrf=nonce, next_path=safe_next(next))
        response.set_cookie(
            LOGIN_CSRF,
            nonce,
            max_age=3600,
            httponly=True,
            samesite="strict",
            secure=request.app.state.settings.secure_cookies,
            path="/login",
        )
        return response

    @router.post("/login")
    def login(
        request: Request,
        login: Annotated[str, Form(max_length=64)],
        password: Annotated[str, Form(max_length=128)],
        csrf: Annotated[str, Form()],
        next: Annotated[str, Form()] = "/",
    ):
        cookie = request.cookies.get(LOGIN_CSRF, "")
        if (
            not cookie
            or len(csrf) > 256
            or not cookie.isascii()
            or not csrf.isascii()
            or not hmac.compare_digest(cookie, csrf)
        ):
            raise HTTPException(403, "Обновите страницу входа")
        try:
            request.app.state.login_signer.loads(csrf, max_age=3600)
        except (BadSignature, SignatureExpired) as exc:
            raise HTTPException(403, "Обновите страницу входа") from exc
        token = request.app.state.identity.authenticate(
            login, password, request.client.host if request.client else "unknown", request.cookies.get(COOKIE)
        )
        if token is None:
            response = render(
                request,
                "login.html",
                login_csrf=csrf,
                next_path=safe_next(next),
                error="Неверный логин или пароль",
                login=login,
            )
            response.status_code = 401
            return response
        response = RedirectResponse(safe_next(next), 303)
        response.set_cookie(
            COOKIE,
            token,
            max_age=request.app.state.settings.session_hours * 3600,
            httponly=True,
            secure=request.app.state.settings.secure_cookies,
            samesite="lax",
            path="/",
        )
        response.delete_cookie(LOGIN_CSRF, path="/login")
        return response

    @router.post("/logout")
    def logout(request: Request, csrf: Annotated[str, Form()]):
        saved = current(request)
        check_csrf(saved, csrf)
        request.app.state.identity.logout(request.cookies[COOKIE], saved)
        response = RedirectResponse("/login", 303)
        response.delete_cookie(COOKIE, path="/")
        return response

    @router.get("/")
    def home(request: Request):
        saved = current(request)
        for section, _, href in (*NAV, *REPORTS):
            if saved.user.can(section):
                return RedirectResponse(href, 303)
        return RedirectResponse("/settings" if saved.user.can("settings") else "/settings/printing", 303)

    def shell(section: str, title: str):
        def endpoint(request: Request):
            saved = current(request)
            saved.user.require(section)
            return render(
                request,
                "section.html",
                saved=saved,
                title=title,
                active_tab=section,
                description="Раздел подключён к новой оболочке. Рабочие операции будут перенесены на следующем этапе.",
            )

        return endpoint

    for section, title, href in (*NAV, *REPORTS):
        if section != "products":
            router.add_api_route(href, shell(section, title), methods=["GET"], name=section)

    @router.get("/settings")
    def settings(request: Request, tab: str = "printing"):
        saved = current(request)
        return settings_page(request, saved, tab)

    @router.get("/settings/printing")
    def printing(request: Request):
        saved = current(request)
        saved.user.require("printing")
        return render(
            request,
            "section.html",
            saved=saved,
            title="Принтеры",
            active_tab="settings",
            description="Профили принтеров будут перенесены вместе с модулем печати. Сейчас задания на печать не создаются.",
        )

    @router.get("/api/v2/me")
    def me(request: Request):
        saved = current(request)
        return {
            "id": str(saved.user.id),
            "login": saved.user.login,
            "role": saved.user.role,
            "sections": sorted(saved.user.sections),
            "csrf": saved.csrf_token,
        }

    @router.get("/api/v2/modules")
    def modules(request: Request):
        current(request).user.require("settings")
        return [{"code": m.code, "title": m.title, "stage": m.stage} for m in MODULES]

    @router.post("/api/v2/users", status_code=201)
    def create_user(request: Request, payload: CreateUser):
        saved = current(request)
        check_csrf(saved, request.headers.get("x-csrf-token", ""))
        user = request.app.state.identity.create_user(
            saved.user,
            payload.login,
            payload.password.get_secret_value(),
            payload.role,
            frozenset(payload.sections),
        )
        return {"id": str(user.id), "login": user.login, "role": user.role, "sections": sorted(user.sections)}

    return router


class CreateUser(BaseModel):
    login: str = Field(min_length=1, max_length=64)
    password: SecretStr
    role: str = "packer"
    sections: list[str] = Field(default_factory=lambda: ["pack", "orders", "returns"], max_length=7)
