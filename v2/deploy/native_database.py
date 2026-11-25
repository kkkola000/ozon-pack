"""Emit native PostgreSQL setup SQL to psql stdin; credentials never become command arguments."""

import argparse
import sys
from pathlib import Path

from psycopg import sql
from sqlalchemy.engine import make_url

from pack_v2.infrastructure.config import load_settings


def provision_statements(role: str, password: str, database: str) -> tuple[str, ...]:
    return tuple(
        statement.as_string()
        for statement in (
            sql.SQL(
                "DO $native_guard$ BEGIN "
                "IF EXISTS (SELECT FROM pg_roles WHERE rolname = {}) "
                "OR EXISTS (SELECT FROM pg_database WHERE datname = {}) THEN "
                "RAISE EXCEPTION 'База или роль v2 уже существует; перезапись запрещена'; "
                "END IF; END $native_guard$;"
            ).format(sql.Literal(role), sql.Literal(database)),
            sql.SQL("SET password_encryption = 'scram-sha-256';"),
            sql.SQL(
                "CREATE ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {};"
            ).format(sql.Identifier(role), sql.Literal(password)),
            sql.SQL("CREATE DATABASE {} OWNER {} ENCODING 'UTF8' TEMPLATE template0;").format(
                sql.Identifier(database), sql.Identifier(role)
            ),
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC;").format(sql.Identifier(database)),
        )
    )


def from_env(path: Path) -> tuple[str, ...]:
    settings = load_settings(path)
    url = make_url(settings.database_url.get_secret_value())
    if (
        url.host != "127.0.0.1"
        or url.username != "ozon_pack_v2"
        or url.database != "ozon_pack_v2"
        or not url.password
        or len(url.password) < 32
        or url.query
    ):
        raise ValueError("Нужна конфигурация отдельной локальной базы v2")
    return provision_statements(url.username, url.password, url.database)


def main() -> int:
    parser = argparse.ArgumentParser(description="Подготовка отдельной системной PostgreSQL для v2")
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        statements = from_env(args.env_file)
    except (OSError, ValueError):
        print("Некорректная конфигурация отдельной базы v2", file=sys.stderr)
        return 2
    print("\n".join(statements))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
