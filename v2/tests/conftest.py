import os
import re

import httpx
import pytest
from alembic import command
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url

from pack_v2.bootstrap.app import create_app
from pack_v2.bootstrap.models import load_models
from pack_v2.cli import migration_config
from pack_v2.infrastructure.config import Settings
from pack_v2.infrastructure.database import Base, Database
from pack_v2.modules.identity.public import Identity

PASSWORD = "Strong-password-123!"


@pytest.fixture(autouse=True)
def no_live_http(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must use a mock transport, not a live marketplace")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)


@pytest.fixture(scope="session")
def settings():
    url = os.environ.get("PACK_V2_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set PACK_V2_TEST_DATABASE_URL to a dedicated PostgreSQL *_test database")
    if not (make_url(url).database or "").endswith("_test"):
        pytest.fail("Refusing to truncate a database whose name does not end with _test")
    return Settings(
        database_url=url,
        secret_key="test-application-secret-" * 3,
        encryption_key=Fernet.generate_key().decode(),
        environment="test",
    )


@pytest.fixture(scope="session")
def migrated(settings):
    load_models()
    command.upgrade(migration_config(settings), "head")


@pytest.fixture
def db(settings, migrated):
    database = Database(settings)
    with database.engine.begin() as conn:
        names = ",".join(f'pack_v2."{table.name}"' for table in Base.metadata.sorted_tables)
        conn.execute(text("TRUNCATE TABLE " + names + " CASCADE"))
    yield database
    database.close()


@pytest.fixture
def identity(db, settings):
    return Identity(db, settings)


@pytest.fixture
def owner(identity):
    return identity.bootstrap_owner("admin", PASSWORD)


@pytest.fixture
def client(settings, db):
    with TestClient(create_app(settings), follow_redirects=False) as client:
        yield client


def login(client, username="admin", password=PASSWORD, next_path="/"):
    response = client.get("/login")
    csrf = re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)
    return client.post(
        "/login", data={"login": username, "password": password, "csrf": csrf, "next": next_path}
    )
