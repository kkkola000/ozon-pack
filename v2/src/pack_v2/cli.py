import argparse
import getpass
import logging
import os
import secrets
import signal
import threading
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from pack_v2.bootstrap.app import SCHEMA_REVISION
from pack_v2.bootstrap.models import load_models
from pack_v2.infrastructure.config import Settings, load_settings
from pack_v2.infrastructure.database import Database
from pack_v2.infrastructure.jobs import Queue, enqueue
from pack_v2.modules.identity.public import Identity
from pack_v2.shared.errors import DomainError


def migration_config(settings: Settings) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent / "migrations"))
    cfg.attributes["settings"] = settings
    return cfg


def generate_env(path: Path) -> None:
    password = secrets.token_urlsafe(32)
    contents = (
        "# Только новая панель. Старый .env не используется.\n"
        "PACK_V2_ENVIRONMENT=development\n"
        f"PACK_V2_POSTGRES_PASSWORD={password}\n"
        f"PACK_V2_DATABASE_URL=postgresql+psycopg://pack_v2:{password}@127.0.0.1:55432/pack_v2\n"
        f"PACK_V2_SECRET_KEY={secrets.token_urlsafe(48)}\n"
        f"PACK_V2_ENCRYPTION_KEY={Fernet.generate_key().decode()}\n"
        'PACK_V2_ALLOWED_HOSTS=["localhost","127.0.0.1"]\n'
        "PACK_V2_SECURE_COOKIES=false\n"
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(contents)
    print(f"Создан {path}; существующие файлы не перезаписываются")


def worker(db: Database, identity: Identity, once: bool) -> None:
    # Only this explicitly idempotent handler is enabled in the foundation.
    queue, stopped = Queue(db), threading.Event()
    scheduled_date = None
    if not once:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stopped.set())
    while not stopped.is_set():
        today = datetime.now(UTC).date().isoformat()
        if today != scheduled_date:
            with db.transaction() as tx:
                enqueue(tx, "identity.cleanup", "identity.cleanup:" + today)
            scheduled_date = today
        job = queue.claim()
        if job:
            if job.kind != "identity.cleanup":
                queue.finish(job, failed=True)
            else:
                try:
                    identity.cleanup()
                except SQLAlchemyError:
                    logging.warning("identity.cleanup delayed; job=%s", job.id)
                    queue.finish(job, retry=True)
                else:
                    queue.finish(job)
        if once:
            return
        if not job:
            stopped.wait(2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Управление отдельной панелью v2")
    parser.add_argument("--env-file", type=Path, help="Явный путь к .env.v2; старый .env не читается")
    commands = parser.add_subparsers(dest="command", required=True)
    env = commands.add_parser("generate-env")
    env.add_argument("--output", type=Path, default=Path(".env.v2"))
    commands.add_parser("migrate")
    commands.add_parser("check")
    owner = commands.add_parser("create-owner")
    owner.add_argument("--login", required=True)
    work = commands.add_parser("worker")
    work.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "generate-env":
            generate_env(args.output)
            return 0
        settings = load_settings(args.env_file)
        load_models()
        if args.command == "migrate":
            command.upgrade(migration_config(settings), "head")
            print("Миграции новой панели применены")
            return 0
        db = Database(settings)
        try:
            if args.command == "check":
                with db.engine.connect() as conn:
                    revision = conn.scalar(text("SELECT version_num FROM pack_v2.alembic_version"))
                if revision != SCHEMA_REVISION:
                    raise DomainError("Примените миграции новой панели")
                print("PostgreSQL и версия схемы готовы")
            elif args.command == "create-owner":
                password = getpass.getpass("Пароль владельца (12–128 символов): ")
                if password != getpass.getpass("Повторите пароль: "):
                    raise DomainError("Пароли не совпадают")
                result = Identity(db, settings).bootstrap_owner(args.login, password)
                print(f"Владелец {result.login} создан")
            elif args.command == "worker":
                worker(db, Identity(db, settings), args.once)
        finally:
            db.close()
    except (DomainError, ValidationError) as exc:
        print(str(exc))
        return 2
    except FileExistsError:
        print("Файл уже существует; перезапись запрещена")
        return 2
    except SQLAlchemyError:
        print("Не удалось обратиться к PostgreSQL. Проверьте отдельную базу v2 и миграции")
        return 2
    return 0
