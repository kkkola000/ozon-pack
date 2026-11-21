import ast
import os
from pathlib import Path

import pytest
from alembic import command
from cryptography.fernet import InvalidToken
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import inspect, text

from pack_v2.bootstrap.app import SCHEMA_REVISION, create_app
from pack_v2.cli import generate_env, migration_config
from pack_v2.infrastructure.config import Settings
from pack_v2.infrastructure.crypto import SecretBox
from pack_v2.infrastructure.database import Base


def test_real_migration_matches_models(settings, db):
    command.check(migration_config(settings))
    inspector = inspect(db.engine)
    assert set(inspector.get_table_names(schema="pack_v2")) == {
        t.name for t in Base.metadata.tables.values()
    } | {"alembic_version"}


def test_migration_round_trip_preserves_unrelated_tables(settings, db):
    with db.engine.begin() as conn:
        conn.execute(text("CREATE TABLE public.v2_migration_sentinel (id integer PRIMARY KEY)"))
        conn.execute(text("INSERT INTO public.v2_migration_sentinel VALUES (7)"))
    try:
        command.downgrade(migration_config(settings), "base")
        with TestClient(create_app(settings)) as application:
            assert application.get("/healthz").status_code == 200
            assert application.get("/readyz").status_code == 503
        assert inspect(db.engine).get_table_names(schema="pack_v2") == ["alembic_version"]
        command.upgrade(migration_config(settings), "head")
        with db.engine.connect() as conn:
            assert conn.scalar(text("SELECT id FROM public.v2_migration_sentinel")) == 7
    finally:
        with db.engine.begin() as conn:
            conn.execute(text("DROP TABLE public.v2_migration_sentinel"))


def test_health_and_readiness(client):
    assert client.get("/healthz").json()["service"] == "pack-v2"
    assert client.get("/readyz").status_code == 200


def test_opening_server_does_not_bootstrap_users(client, db):
    client.get("/healthz")
    client.get("/login")
    with db.engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM pack_v2.users")) == 0


def test_readiness_rejects_schema_drift(client, db):
    with db.engine.begin() as conn:
        conn.execute(text("UPDATE pack_v2.alembic_version SET version_num='not-this-version'"))
    try:
        assert client.get("/readyz").status_code == 503
        assert client.get("/healthz").status_code == 200
    finally:
        with db.engine.begin() as conn:
            conn.execute(
                text("UPDATE pack_v2.alembic_version SET version_num=:revision"),
                {"revision": SCHEMA_REVISION},
            )


def test_wrong_host_rejected(client):
    assert client.get("/healthz", headers={"host": "attacker.example"}).status_code == 400


def test_configuration_rejects_legacy_sqlite(settings):
    fields = settings.model_dump()
    fields["database_url"] = "sqlite:////opt/ozon-pack/data/panel.db"
    with pytest.raises(ValidationError):
        Settings(**fields)


def test_root_dotenv_not_loaded(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith("PACK_V2_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("PACK_V2_SECRET_KEY=" + "a" * 64 + "\n")
    with pytest.raises(ValidationError) as result:
        Settings()
    assert any(e["loc"] == ("secret_key",) for e in result.value.errors())


def test_production_requires_secure_cookies(settings):
    fields = settings.model_dump()
    fields.update(environment="production", secure_cookies=False)
    with pytest.raises(ValidationError):
        Settings(**fields)


def test_generated_config_is_private_and_never_overwritten(tmp_path):
    path = tmp_path / ".env.v2"
    generate_env(path)
    original = path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        generate_env(path)
    assert path.read_bytes() == original
    assert Settings(_env_file=path).database_url.get_secret_value().endswith("/pack_v2")


def test_secret_box_preserves_exact_mark_payload(settings):
    payload = b' \x1d010000000000000021TEST"SERIAL123\x1d91ABCD\x1d92example+crypto/== '
    box = SecretBox(settings.encryption_key.get_secret_value())
    encrypted = box.seal(payload)
    assert encrypted != payload and box.open(encrypted) == payload
    from cryptography.fernet import Fernet

    with pytest.raises(InvalidToken):
        SecretBox(Fernet.generate_key().decode()).open(encrypted)


def test_module_dependencies_do_not_import_legacy_or_private_neighbors():
    root = Path(__file__).resolve().parents[1] / "src" / "pack_v2"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        own = path.relative_to(root).parts
        for node in ast.walk(tree):
            modules = (
                [node.module]
                if isinstance(node, ast.ImportFrom)
                else [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            for module in modules:
                assert not module or not (module == "app" or module.startswith("app.")), str(path)
                if own[0] == "modules" and module and module.startswith("pack_v2.modules."):
                    target = module.split(".")
                    assert target[2] == own[1] or target[3:] == ["public"], str(path)
