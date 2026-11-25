import hmac
from urllib.parse import quote

from fastapi import HTTPException, Request

from pack_v2.modules.identity.public import Authenticated

COOKIE = "pack_v2_session"
LOGIN_CSRF = "pack_v2_login_csrf"


def current(request: Request) -> Authenticated:
    saved = request.app.state.identity.current(request.cookies.get(COOKIE))
    if not saved:
        if request.url.path.startswith("/api/"):
            raise HTTPException(401, "Войдите в панель")
        raise HTTPException(303, headers={"Location": "/login?next=" + quote(request.url.path, safe="/")})
    request.state.current = saved
    return saved


def check_csrf(saved: Authenticated, token: str) -> None:
    if (
        not token
        or len(token) > 128
        or not token.isascii()
        or not hmac.compare_digest(saved.csrf_token, token)
    ):
        raise HTTPException(403, "Обновите страницу и повторите действие")


def manager(request: Request, token: str | None = None) -> Authenticated:
    saved = current(request)
    saved.user.require("settings")
    if token is not None:
        check_csrf(saved, token)
    return saved
