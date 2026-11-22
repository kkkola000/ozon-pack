"""Native PostgreSQL configuration, isolated provisioning and protection of existing resources."""

import importlib.util
import secrets
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from psycopg import sql
from sqlalchemy.engine import make_url

from pack_v2.cli import main, migration_config
from pack_v2.infrastructure.config import Settings

spec = importlib.util.spec_from_file_location(
    "native_database", Path(__file__).resolve().parents[1] / "deploy" / "native_database.py"
)
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


def test_native_config_is_generated_by_cli_with_private_credentials(tmp_path):
    path = tmp_path / ".env.v2"
    assert main(["generate-env", "--native-postgres", "--database-port", "5433", "--output", str(path)]) == 0
    settings = Settings(_env_file=path)
    url = make_url(settings.database_url.get_secret_value())
    assert url.host == "127.0.0.1" and url.port == 5433
    assert url.username == url.database == "ozon_pack_v2"
    assert len(url.password) >= 32
    assert path.stat().st_mode & 0o777 == 0o600
    assert "PACK_V2_POSTGRES_PASSWORD=" not in path.read_text()
    original = path.read_bytes()
    assert main(["generate-env", "--native-postgres", "--output", str(path)]) == 2
    assert path.read_bytes() == original
    assert len(native.from_env(path)) == 5


@pytest.mark.parametrize("port", ["0", "65536"])
def test_invalid_native_port_does_not_leave_config(tmp_path, port):
    path = tmp_path / ".env.v2"
    assert main(["generate-env", "--native-postgres", "--database-port", port, "--output", str(path)]) == 2
    assert not path.exists()


@pytest.mark.parametrize("change", ["host", "username", "database", "query"])
def test_provisioner_refuses_config_for_another_database(tmp_path, change):
    path = tmp_path / ".env.v2"
    main(["generate-env", "--native-postgres", "--output", str(path)])
    content = path.read_text()
    if change == "host":
        content = content.replace("@127.0.0.1:", "@another-host:")
    elif change == "username":
        content = content.replace("//ozon_pack_v2:", "//another_user:")
    elif change == "database":
        content = content.replace("/ozon_pack_v2\n", "/another_database\n")
    else:
        content = content.replace("/ozon_pack_v2\n", "/ozon_pack_v2?options=another\n")
    path.write_text(content)
    with pytest.raises(ValueError):
        native.from_env(path)


@pytest.fixture
def native_admin(settings):
    url = make_url(settings.database_url.get_secret_value()).set(drivername="postgresql")
    with psycopg.connect(url.render_as_string(hide_password=False), autocommit=True) as admin:
        if not admin.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user").fetchone()[0]:
            pytest.skip("Provisioning requires an administrator in the dedicated test cluster")
        yield admin, url


def test_native_role_can_migrate_and_repeated_provisioning_keeps_data(native_admin, settings):
    admin, admin_url = native_admin
    suffix = uuid4().hex[:16]
    role, database = "v2_native_" + suffix, "v2_native_" + suffix + "_test"
    password = secrets.token_urlsafe(32) + "'\\safe"
    role_created = db_created = False
    try:
        for index, statement in enumerate(native.provision_statements(role, password, database)):
            admin.execute(statement)
            if index == 2:
                role_created = True
            elif index == 3:
                db_created = True
        login = admin_url.set(username=role, password=password, database=database)
        fields = settings.model_dump()
        fields["database_url"] = login.set(drivername="postgresql+psycopg").render_as_string(
            hide_password=False
        )
        command.upgrade(migration_config(Settings(**fields)), "head")
        with psycopg.connect(login.render_as_string(hide_password=False), autocommit=True) as client:
            flags = client.execute(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication FROM pg_roles WHERE rolname=current_user"
            ).fetchone()
            assert flags == (False, False, False, False)
            assert client.execute("SELECT version_num FROM pack_v2.alembic_version").fetchone()[0]
            client.execute("CREATE TABLE pack_v2.native_install_sentinel (id integer)")
            client.execute("INSERT INTO pack_v2.native_install_sentinel VALUES (42)")
            with pytest.raises(psycopg.errors.RaiseException, match="уже существует"):
                admin.execute(native.provision_statements(role, "changed-password", database)[0])
            assert client.execute("SELECT id FROM pack_v2.native_install_sentinel").fetchone() == (42,)
    finally:
        if db_created:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
        if role_created:
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


@pytest.mark.parametrize("existing", ["role", "database"])
def test_provisioner_refuses_either_existing_resource(native_admin, existing):
    admin, _ = native_admin
    suffix = uuid4().hex[:16]
    role, database = "v2_native_" + suffix, "v2_native_" + suffix + "_test"
    if existing == "role":
        admin.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
    else:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        with pytest.raises(psycopg.errors.RaiseException, match="уже существует"):
            admin.execute(native.provision_statements(role, "unused", database)[0])
        if existing == "role":
            assert not admin.execute("SELECT FROM pg_database WHERE datname=%s", (database,)).fetchall()
        else:
            assert not admin.execute("SELECT FROM pg_roles WHERE rolname=%s", (role,)).fetchall()
    finally:
        if existing == "role":
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        else:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
