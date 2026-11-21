from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from conftest import PASSWORD, login
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from pack_v2.bootstrap.app import create_app
from pack_v2.modules.identity.models import LoginSession, User, UserSection
from pack_v2.shared.errors import Conflict, Forbidden


def test_login_session_is_opaque_and_server_side(client, owner, db):
    result = login(client)
    assert result.status_code == 303
    token = client.cookies.get("pack_v2_session")
    assert token and "admin" not in token
    with db.transaction() as tx:
        saved = tx.scalar(select(LoginSession))
        user = tx.scalar(select(User))
        assert saved.token_digest != token
        assert user.password_hash.startswith("$argon2id$")
        assert PASSWORD not in user.password_hash
    me = client.get("/api/v2/me")
    assert me.status_code == 200
    assert me.json()["role"] == "owner"
    assert "password" not in me.text and "token_digest" not in me.text


def test_unauthenticated_access(client):
    assert client.get("/api/v2/me").status_code == 401
    result = client.get("/orders")
    assert result.status_code == 303
    assert result.headers["location"].startswith("/login")


def test_oversized_auth_bodies_are_rejected_before_parsing(client):
    result = client.post(
        "/login", content=b"x" * 20_000, headers={"content-type": "application/x-www-form-urlencoded"}
    )
    assert result.status_code == 413
    result = client.post(
        "/api/v2/users",
        content=iter([b"x" * 9_000, b"x" * 9_000]),
        headers={"content-type": "application/json"},
    )
    assert result.status_code == 413


def test_login_requires_signed_csrf(client, owner):
    result = client.post("/login", data={"login": "admin", "password": PASSWORD, "csrf": "fake"})
    assert result.status_code == 403


def test_non_ascii_csrf_is_rejected_without_server_error(client, owner):
    client.get("/login")
    result = client.post("/login", data={"login": "admin", "password": PASSWORD, "csrf": "не токен"})
    assert result.status_code == 403
    login(client)
    assert client.post("/logout", data={"csrf": "не токен"}).status_code == 403
    client.cookies.set("pack_v2_login_csrf", "forged")
    result = client.post("/login", data={"login": "admin", "password": PASSWORD, "csrf": "forged"})
    assert result.status_code == 403


def test_wrong_password_and_unknown_user_have_same_message(client, owner):
    first = login(client, password="wrong-password").text
    second = login(client, username="missing", password="wrong-password").text
    assert "Неверный логин или пароль" in first and "Неверный логин или пароль" in second
    assert not client.cookies.get("pack_v2_session")


def test_invalid_login_cannot_authenticate_existing_invalid_name(client, identity):
    identity.bootstrap_owner("invalid", PASSWORD)
    assert login(client, username="invalid!?").status_code == 401


def test_normalized_login_is_unique(identity, owner):
    with pytest.raises(Conflict):
        identity.create_user(owner, " ADMIN ", PASSWORD, "packer", frozenset({"pack"}))


def test_bootstrap_cannot_create_second_owner(identity, owner):
    with pytest.raises(Conflict):
        identity.bootstrap_owner("another-owner", PASSWORD)


def test_concurrent_bootstrap_creates_only_one_first_owner(identity, db):
    def create(number):
        try:
            identity.bootstrap_owner(f"owner-{number}", PASSWORD)
            return "created"
        except Conflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(create, range(2))) == ["conflict", "created"]
    with db.transaction() as tx:
        assert tx.scalar(select(func.count()).select_from(User)) == 1


@pytest.mark.parametrize(
    "target", ["https://example.com", "//example.com", "/\\example.com", "/pack\r\nX: a"]
)
def test_login_redirect_stays_local(client, owner, target):
    result = login(client, next_path=target)
    assert result.status_code == 303 and result.headers["location"] == "/"


def test_logout_is_post_and_csrf_protected(client, owner):
    login(client)
    token = client.cookies.get("pack_v2_session")
    assert client.get("/logout").status_code == 405
    assert client.post("/logout", data={"csrf": "bad"}).status_code == 403
    csrf = client.get("/api/v2/me").json()["csrf"]
    assert client.post("/logout", data={"csrf": csrf}).status_code == 303
    client.cookies.set("pack_v2_session", token)
    assert client.get("/api/v2/me").status_code == 401


@pytest.mark.parametrize("change", ["inactive", "expired", "password_changed"])
def test_session_revocation_is_immediate(client, owner, db, change):
    login(client)
    with db.transaction() as tx:
        user = tx.get(User, owner.id)
        if change == "inactive":
            user.active = False
        elif change == "password_changed":
            user.auth_revision += 1
        else:
            tx.scalar(select(LoginSession)).expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert client.get("/api/v2/me").status_code == 401


def test_worker_only_sees_granted_sections(client, identity, owner):
    identity.create_user(owner, "packer", PASSWORD, "packer", frozenset({"pack"}))
    login(client, "packer")
    page = client.get("/pack")
    assert page.status_code == 200
    assert 'href="/orders"' not in page.text
    assert client.get("/orders").status_code == 403
    assert client.get("/settings").status_code == 403
    assert client.get("/settings/printing").status_code == 200
    assert client.get("/api/v2/modules").status_code == 403
    csrf = client.get("/api/v2/me").json()["csrf"]
    result = client.post(
        "/api/v2/users", headers={"X-CSRF-Token": csrf}, json={"login": "other", "password": PASSWORD}
    )
    assert result.status_code == 403


def test_section_change_applies_to_existing_session(client, identity, owner, db):
    worker = identity.create_user(owner, "packer", PASSWORD, "packer", frozenset({"orders"}))
    login(client, "packer")
    assert client.get("/orders").status_code == 200
    with db.transaction() as tx:
        tx.delete(tx.get(UserSection, (worker.id, "orders")))
    assert client.get("/orders").status_code == 403


def test_admin_cannot_create_owner(identity, owner):
    admin = identity.create_user(owner, "manager", PASSWORD, "admin", frozenset())
    assert admin.can("settings") and not admin.can("orders")
    with pytest.raises(Forbidden):
        identity.create_user(admin, "another", PASSWORD, "owner", frozenset())


def test_create_user_api_requires_csrf_and_does_not_return_password(client, owner):
    login(client)
    payload = {"login": "worker", "password": PASSWORD, "sections": ["pack"]}
    assert client.post("/api/v2/users", json=payload).status_code == 403
    csrf = client.get("/api/v2/me").json()["csrf"]
    result = client.post("/api/v2/users", headers={"X-CSRF-Token": csrf}, json=payload)
    assert result.status_code == 201 and result.json()["sections"] == ["pack"]
    assert PASSWORD not in result.text and "password" not in result.text


def test_login_rate_limit_is_shared_by_app_instances(settings, db, owner):
    limited = settings.model_copy(update={"login_limit": 2})
    with TestClient(create_app(limited), follow_redirects=False) as first:
        assert login(first, password="bad").status_code == 401
    with TestClient(create_app(limited), follow_redirects=False) as second:
        assert login(second, password="bad").status_code == 401
        result = login(second, password=PASSWORD)
        assert result.status_code == 429 and "retry-after" in result.headers


def test_security_headers_and_secure_production_cookie(settings, db, owner):
    production = settings.model_copy(update={"environment": "production", "secure_cookies": True})
    with TestClient(create_app(production), base_url="https://testserver", follow_redirects=False) as client:
        result = login(client)
        header = result.headers["set-cookie"].lower()
        assert "secure" in header and "httponly" in header and "samesite=lax" in header
        assert "frame-ancestors 'none'" in result.headers["content-security-policy"]
        assert result.headers["cache-control"] == "no-store"
