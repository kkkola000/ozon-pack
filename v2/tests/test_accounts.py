import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from conftest import PASSWORD, login
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import func, select

from pack_v2.infrastructure.crypto import SecretBox
from pack_v2.infrastructure.models import AuditEvent
from pack_v2.integrations.contracts import CapabilityCheck
from pack_v2.integrations.models import AccountCheck, AccountSecret, MarketplaceAccount
from pack_v2.integrations.public import Connections
from pack_v2.modules.orders.models import Order
from pack_v2.shared.errors import Conflict, DomainError, Forbidden

KEY = "synthetic-api-key-not-for-real-ozon"


def results(status="passed"):
    return [
        CapabilityCheck(code, title, status, "Доступ подтверждён" if status == "passed" else "Нет доступа")
        for code, title in (("orders", "Заказы FBS"), ("products", "Товары"), ("returns", "Возвраты"))
    ]


class Checker:
    def __init__(self):
        self.calls = []
        self.results = results()

    def check(self, credentials):
        self.calls.append(credentials)
        return self.results


@pytest.fixture
def checker():
    return Checker()


@pytest.fixture
def connections(db, settings, checker):
    return Connections(db, SecretBox(settings.encryption_key.get_secret_value()), checker)


@pytest.fixture
def account_id(connections, owner):
    return connections.create(owner, "ANEX", "000123", SecretStr(KEY))


@pytest.fixture
def account_client(client, connections, owner):
    client.app.state.connections = connections
    login(client)
    return client


def csrf(client):
    return client.get("/api/v2/me").json()["csrf"]


def test_credentials_encrypted_and_never_returned(db, settings, connections, account_id, owner, checker):
    with db.transaction() as tx:
        rows = tx.scalars(select(AccountSecret)).all()
        values = {r.kind: r.ciphertext for r in rows}
        assert values["api_key"] != KEY.encode() and values["client_id"] != b"000123"
        box = SecretBox(settings.encryption_key.get_secret_value())
        assert box.open(values["api_key"]) == KEY.encode()
        assert box.open(values["client_id"]) == b"000123"
        assert tx.scalar(select(func.count()).select_from(Order)) == 0
        audit = tx.scalar(select(AuditEvent))
        assert KEY not in json.dumps(audit.details)
    view = connections.list_accounts(owner)[0]
    assert view.id == account_id and view.client_id == "000123"
    assert view.key_saved and view.status == "unverified"
    assert KEY not in str(asdict(view))
    assert checker.calls == []


def test_duplicate_client_id_canonicalized_even_if_disabled(connections, account_id, owner, db):
    connections.set_active(owner, account_id, 0, False)
    with pytest.raises(Conflict):
        connections.create(owner, "Другой кабинет", "123", SecretStr("different-key"))
    with db.transaction() as tx:
        assert tx.scalar(select(func.count()).select_from(MarketplaceAccount)) == 1


@pytest.mark.parametrize("client_id", ["", "0", "000", "abc", "١٢٣", "123\r\nheader", "1" * 33])
def test_invalid_client_ids_rejected(connections, owner, client_id):
    with pytest.raises(DomainError):
        connections.create(owner, "Кабинет", client_id, SecretStr(KEY))


@pytest.mark.parametrize("key", ["", "bad key", "bad\r\nkey", "кириллица", "k" * 513])
def test_invalid_keys_rejected_without_echo(connections, owner, key):
    with pytest.raises(DomainError) as error:
        connections.create(owner, "Кабинет", "123", SecretStr(key))
    if key:
        assert key not in str(error.value)


def test_blank_key_retains_secret_and_verified_access(connections, owner, account_id, db):
    connections.verify(owner, account_id, 0)
    with db.transaction() as tx:
        original = tx.get(AccountSecret, (account_id, "api_key")).ciphertext
    connections.update(owner, account_id, 0, "Переименован", SecretStr(""))
    with db.transaction() as tx:
        assert tx.get(AccountSecret, (account_id, "api_key")).ciphertext == original
    view = connections.list_accounts(owner)[0]
    assert view.title == "Переименован" and view.revision == 1 and view.status == "passed"


def test_key_rotation_invalidates_check_and_stale_form_cannot_overwrite(connections, owner, account_id):
    connections.verify(owner, account_id, 0)
    connections.update(owner, account_id, 0, "ANEX", SecretStr("replacement-key"))
    assert connections.list_accounts(owner)[0].status == "unverified"
    with pytest.raises(Conflict):
        connections.update(owner, account_id, 0, "Stale", SecretStr(KEY))
    with pytest.raises(Conflict):
        connections.verify(owner, account_id, 0)


def test_inactive_account_preserves_history_and_keys(connections, owner, account_id, db):
    connections.verify(owner, account_id, 0)
    connections.set_active(owner, account_id, 0, False)
    view = connections.list_accounts(owner)[0]
    assert not view.active and view.status == "passed" and view.key_saved
    connections.set_active(owner, account_id, 1, True)
    assert connections.list_accounts(owner)[0].active
    with db.transaction() as tx:
        assert tx.scalar(select(func.count()).select_from(AccountCheck)) == 1


@pytest.mark.parametrize(
    "statuses,expected",
    [
        (["passed"] * 3, "passed"),
        (["passed", "denied", "denied"], "partial"),
        (["denied"] * 3, "failed"),
        (["unauthorized"] * 3, "failed"),
        (["unavailable"] * 3, "unavailable"),
    ],
)
def test_partial_access_does_not_look_fully_verified(
    connections, checker, owner, account_id, statuses, expected
):
    checker.results = [
        CapabilityCheck(r.code, r.title, s, "safe") for r, s in zip(results(), statuses, strict=True)
    ]
    connections.verify(owner, account_id, 0)
    view = connections.list_accounts(owner)[0]
    assert view.status == expected and view.checked_at is not None
    assert [r["status"] for r in view.results] == statuses


def test_incomplete_report_never_confirms_full_access(connections, checker, owner, account_id):
    checker.results = results()[:1]
    connections.verify(owner, account_id, 0)
    assert connections.list_accounts(owner)[0].status == "unavailable"


def test_lost_encryption_key_is_handled_without_exposing_secrets(db, checker, connections, owner, account_id):
    connections.verify(owner, account_id, 0)
    broken = Connections(db, SecretBox(Fernet.generate_key().decode()), checker)
    view = broken.list_accounts(owner)[0]
    assert view.configuration_error and view.client_id is None and view.status == "unverified"
    with pytest.raises(DomainError):
        broken.verify(owner, account_id, 0)
    with db.transaction() as tx:
        original = tx.get(AccountSecret, (account_id, "api_key")).ciphertext
    with pytest.raises(DomainError):
        broken.update(owner, account_id, 0, "Wrong key", SecretStr("replacement"))
    with db.transaction() as tx:
        assert tx.get(AccountSecret, (account_id, "api_key")).ciphertext == original
        assert tx.get(MarketplaceAccount, account_id).revision == 0


def test_pending_check_does_not_lock_database_and_old_result_cannot_confirm_new_key(db, settings, owner):
    entered, release = Event(), Event()

    class WaitingChecker:
        def check(self, credentials):
            entered.set()
            assert release.wait(10)
            return results()

    connections = Connections(db, SecretBox(settings.encryption_key.get_secret_value()), WaitingChecker())
    account_id = connections.create(owner, "ANEX", "123", SecretStr(KEY))
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(connections.verify, owner, account_id, 0)
        assert entered.wait(5)
        try:
            assert db.engine.pool.checkedout() == 0
            with pytest.raises(Conflict):
                connections.verify(owner, account_id, 0)
            connections.update(owner, account_id, 0, "ANEX", SecretStr("new-key"))
        finally:
            release.set()
        with pytest.raises(Conflict):
            running.result(timeout=5)
    assert connections.list_accounts(owner)[0].status == "unverified"


def test_expired_check_can_be_replaced(connections, owner, account_id, db):
    with db.transaction() as tx:
        tx.add(
            AccountCheck(
                account_id=account_id,
                credential_revision=1,
                lease_until=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
    connections.verify(owner, account_id, 0)
    assert connections.list_accounts(owner)[0].status == "passed"
    with db.transaction() as tx:
        assert set(tx.scalars(select(AccountCheck.status))) == {"stale", "passed"}


def test_worker_forbidden_for_direct_service_and_http(identity, connections, account_id, owner, client):
    packer = identity.create_user(owner, "worker", PASSWORD, "packer", frozenset({"pack"}))
    with pytest.raises(Forbidden):
        connections.list_accounts(packer)
    with pytest.raises(Forbidden):
        connections.create(packer, "Hidden", "321", SecretStr(KEY))
    client.app.state.connections = connections
    login(client, "worker")
    assert client.get("/api/v2/accounts").status_code == 403
    assert client.get("/settings?tab=accounts").status_code == 403
    assert (
        client.post(
            "/api/v2/accounts",
            json={"title": "X", "client_id": "123", "api_key": KEY},
            headers={"x-csrf-token": csrf(client)},
        ).status_code
        == 403
    )


def test_account_api_lifecycle_is_csrf_protected(account_client, connections, checker):
    client = account_client
    payload = {"title": "ANEX", "client_id": "000123", "api_key": KEY}
    assert client.post("/api/v2/accounts", json=payload).status_code == 403
    headers = {"x-csrf-token": csrf(client)}
    response = client.post("/api/v2/accounts", json=payload, headers=headers)
    assert response.status_code == 201
    account_id = response.json()["id"]
    assert KEY not in client.get("/api/v2/accounts").text
    assert KEY not in client.get("/settings?tab=accounts").text
    assert client.post(f"/api/v2/accounts/{account_id}/check", json={"revision": 0}).status_code == 403
    response = client.post(f"/api/v2/accounts/{account_id}/check", json={"revision": 0}, headers=headers)
    assert response.status_code == 200 and len(response.json()["results"]) == 3
    response = client.patch(
        f"/api/v2/accounts/{account_id}",
        json={"title": "Новое", "revision": 0, "api_key": ""},
        headers=headers,
    )
    assert response.status_code == 200
    assert (
        client.patch(
            f"/api/v2/accounts/{account_id}", json={"title": "Старое", "revision": 0}, headers=headers
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/v2/accounts/{account_id}/active", json={"revision": 1, "active": False}, headers=headers
        ).status_code
        == 200
    )
    assert not client.get("/api/v2/accounts").json()[0]["active"]
    assert len(checker.calls) == 1


def test_client_id_cannot_be_reassigned_and_validation_never_echoes_key(account_client, account_id):
    client = account_client
    headers = {"x-csrf-token": csrf(client)}
    bad = client.patch(
        f"/api/v2/accounts/{account_id}",
        json={"revision": 0, "title": "Change", "client_id": "999", "api_key": KEY},
        headers=headers,
    )
    assert bad.status_code == 422 and KEY not in bad.text
    missing = client.post("/api/v2/accounts", json={"api_key": KEY}, headers=headers)
    assert missing.status_code == 422 and KEY not in missing.text
    assert client.get("/api/v2/accounts").json()[0]["client_id"] == "000123"


def test_html_save_redirect_and_notice_are_real(account_client, checker):
    client = account_client
    data = {
        "csrf": csrf(client),
        "title": "Кабинет",
        "client_id": "321",
        "api_key": KEY,
        "marketplace": "ozon",
    }
    result = client.post("/settings/accounts", data=data)
    assert result.status_code == 303
    page = client.get(result.headers["location"])
    assert "Кабинет сохранён" in page.text and "321" in page.text and KEY not in page.text
    assert client.get("/api/v2/accounts").json()[0]["title"] == "Кабинет"
    assert checker.calls == []


def test_duplicate_html_error_does_not_return_secret(account_client, account_id):
    client = account_client
    result = client.post(
        "/settings/accounts",
        data={
            "csrf": csrf(client),
            "title": "Duplicate",
            "client_id": "123",
            "api_key": KEY,
            "marketplace": "ozon",
        },
    )
    assert result.status_code == 400 and "уже добавлен" in result.text
    assert KEY not in result.text


def test_unknown_account_and_oversized_requests(account_client):
    client = account_client
    headers = {"x-csrf-token": csrf(client)}
    assert (
        client.post(f"/api/v2/accounts/{uuid4()}/check", json={"revision": 0}, headers=headers).status_code
        == 404
    )
    response = client.patch(f"/api/v2/accounts/{uuid4()}", content=b"x" * 20_000, headers=headers)
    assert response.status_code == 413
