from uuid import uuid4

import httpx
import pytest
from conftest import PASSWORD
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import func, select

from pack_v2.infrastructure.crypto import SecretBox
from pack_v2.infrastructure.database import Base
from pack_v2.integrations.ozon.access import OzonAccessChecker
from pack_v2.integrations.ozon.reader import OzonReadAdapter
from pack_v2.integrations.public import AccountSource, Connections, ReadError
from pack_v2.shared.errors import Conflict, DomainError, Forbidden, NotFound

KEY = "synthetic-read-key"


@pytest.fixture
def connected(db, settings, owner):
    clients, calls = [], []

    def factory(source, credentials):
        assert db.engine.pool.checkedout() == 0

        def handle(request):
            assert db.engine.pool.checkedout() == 0
            calls.append((source, request.headers["client-id"], request.headers["api-key"]))
            return httpx.Response(200, json={"result": {"items": []}})

        reader = OzonReadAdapter(source, credentials, transport=httpx.MockTransport(handle))
        clients.append(reader)
        return reader

    service = Connections(
        db, SecretBox(settings.encryption_key.get_secret_value()), OzonAccessChecker(), factory
    )
    account = service.create(owner, "Синтетический кабинет", "000123", SecretStr(KEY))
    return service, account, clients, calls


def table_counts(db):
    with db.transaction() as tx:
        return {
            name: tx.scalar(select(func.count()).select_from(table))
            for name, table in Base.metadata.tables.items()
        }


def test_read_uses_only_its_encrypted_account_credentials_and_writes_no_business_data(connected, owner, db):
    service, first, clients, calls = connected
    second = service.create(owner, "Второй", "000456", SecretStr("second-read-key"))
    before = table_counts(db)
    for account in (first, second):
        with service.reader(owner, account) as reader:
            page = next(reader.iter_catalog())
            assert page.source == AccountSource(account, "ozon", 1)
            assert page.is_last
    assert [(source.account_id, client_id, key) for source, client_id, key in calls] == [
        (first, "000123", KEY),
        (second, "000456", "second-read-key"),
    ]
    assert all(reader.http.client.is_closed for reader in clients)
    assert table_counts(db) == before


@pytest.mark.parametrize("change", ["key", "disabled", "title"])
def test_credential_rotation_and_disable_reject_stale_read_but_title_change_keeps_source(
    connected, owner, change
):
    service, account, clients, _ = connected

    def read_and_change():
        with service.reader(owner, account) as reader:
            next(reader.iter_catalog())
            if change == "disabled":
                service.set_active(owner, account, 0, False)
            else:
                service.update(
                    owner,
                    account,
                    0,
                    "Новое название",
                    SecretStr("replacement-key") if change == "key" else None,
                )

    if change == "title":
        read_and_change()
    else:
        with pytest.raises(Conflict):
            read_and_change()
    assert clients[-1].http.client.is_closed


def test_disabled_account_cannot_start_read_and_unknown_account_does_not_fall_back(connected, owner):
    service, account, clients, _ = connected
    service.set_active(owner, account, 0, False)
    with pytest.raises(Conflict):
        with service.reader(owner, account):
            pytest.fail("Inactive account was opened")
    with pytest.raises(NotFound):
        with service.reader(owner, uuid4()):
            pytest.fail("Unknown account was opened")
    assert not clients


def test_worker_cannot_decrypt_account_or_start_read(connected, owner, identity):
    service, account, clients, _ = connected
    packer = identity.create_user(owner, "worker-reader", PASSWORD, "packer", frozenset({"pack"}))
    with pytest.raises(Forbidden):
        with service.reader(packer, account):
            pytest.fail("Worker should not open integration reader")
    with pytest.raises(Forbidden):
        service.assert_source_current(packer, AccountSource(account, "ozon", 1))
    assert not clients


def test_wrong_encryption_key_stops_before_http(connected, owner, db):
    service, account, clients, _ = connected
    broken = Connections(
        db, SecretBox(Fernet.generate_key().decode()), OzonAccessChecker(), service.reader_factory
    )
    with pytest.raises(DomainError) as error:
        with broken.reader(owner, account):
            pytest.fail("Decryption failure was ignored")
    assert KEY not in str(error.value) and not clients


def test_exception_closes_reader_and_preserves_safe_error(connected, owner):
    service, account, clients, _ = connected
    with pytest.raises(ReadError) as error:
        with service.reader(owner, account):
            raise ReadError("incomplete")
    assert error.value.kind == "incomplete" and clients[-1].http.client.is_closed


def test_source_validation_rejects_wrong_provider_or_key_revision(connected, owner):
    service, account, _, _ = connected
    service.assert_source_current(owner, AccountSource(account, "ozon", 1))
    for source in (AccountSource(account, "yandex", 1), AccountSource(account, "ozon", 2)):
        with pytest.raises(Conflict):
            service.assert_source_current(owner, source)
