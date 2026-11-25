import argparse
import getpass
import logging
import os
import secrets
import signal
import threading
import time
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
from pack_v2.infrastructure.crypto import SecretBox
from pack_v2.infrastructure.database import Database
from pack_v2.infrastructure.jobs import Queue, enqueue
from pack_v2.integrations.ozon.access import OzonAccessChecker
from pack_v2.integrations.ozon.documents import OzonLabelAdapter
from pack_v2.integrations.ozon.instances import OzonInstanceAdapter
from pack_v2.integrations.ozon.reader import OzonReadAdapter
from pack_v2.integrations.ozon.shipping import OzonShippingAdapter
from pack_v2.integrations.public import Connections
from pack_v2.modules.assembly.readiness import Readiness
from pack_v2.modules.catalog.public import Catalog
from pack_v2.modules.documents.archive import StickerArchive
from pack_v2.modules.documents.public import Documents
from pack_v2.modules.identity.public import Identity
from pack_v2.modules.instance_data.public import InstanceData
from pack_v2.modules.marking.public import Marking
from pack_v2.modules.orders.public import Orders
from pack_v2.shared.errors import DomainError


def migration_config(settings: Settings) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent / "migrations"))
    cfg.attributes["settings"] = settings
    return cfg


def generate_env(path: Path, *, native_postgres: bool = False, database_port: int | None = None) -> None:
    port = database_port if database_port is not None else (5432 if native_postgres else 55432)
    if not 1 <= port <= 65535:
        raise DomainError("Порт PostgreSQL должен быть от 1 до 65535")
    database = "ozon_pack_v2" if native_postgres else "pack_v2"
    password = secrets.token_urlsafe(32)
    contents = (
        "# Только новая панель. Старый .env не используется.\n"
        "PACK_V2_ENVIRONMENT=development\n"
        + ("" if native_postgres else f"PACK_V2_POSTGRES_PASSWORD={password}\n")
        + f"PACK_V2_DATABASE_URL=postgresql+psycopg://{database}:{password}@127.0.0.1:{port}/{database}\n"
        f"PACK_V2_SECRET_KEY={secrets.token_urlsafe(48)}\n"
        f"PACK_V2_ENCRYPTION_KEY={Fernet.generate_key().decode()}\n"
        'PACK_V2_ALLOWED_HOSTS=["localhost","127.0.0.1"]\n'
        "PACK_V2_SECURE_COOKIES=false\n"
        "PACK_V2_CATALOG_SYNC_MINUTES=15\n"
        "PACK_V2_ORDERS_SYNC_MINUTES=5\n"
        "PACK_V2_ORDERS_LOOKBACK_DAYS=30\n"
        "PACK_V2_SPLIT_ORDERS_ON_PACK=true\n"
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(contents)
    print(f"Создан {path}; существующие файлы не перезаписываются")


def worker(
    db: Database,
    identity: Identity,
    once: bool,
    catalog: Catalog | None = None,
    catalog_minutes: int = 15,
    orders: Orders | None = None,
    orders_minutes: int = 5,
    instance_data: InstanceData | None = None,
    readiness: Readiness | None = None,
    documents: Documents | None = None,
    archive: StickerArchive | None = None,
) -> None:
    # Instance writes use durable dispatch intents and reconcile ambiguous outcomes.
    queue, stopped = Queue(db), threading.Event()
    scheduled_date = None
    scheduled_at = {}
    if not once:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stopped.set())
    while not stopped.is_set():
        today = datetime.now(UTC).date().isoformat()
        if today != scheduled_date:
            with db.transaction() as tx:
                enqueue(tx, "identity.cleanup", "identity.cleanup:" + today)
            if archive:
                archive.schedule()
            scheduled_date = today
        for service, minutes, kind in (
            (catalog, catalog_minutes, "catalog.sync"),
            (orders, orders_minutes, "orders.sync"),
        ):
            if service and (kind not in scheduled_at or time.monotonic() - scheduled_at[kind] >= 60):
                try:
                    service.schedule(minutes)
                except SQLAlchemyError:
                    logging.warning("%s scheduler delayed", kind)
                else:
                    scheduled_at[kind] = time.monotonic()
        job = queue.claim(lease_seconds=120)
        if job:
            service = {
                "catalog.sync": catalog,
                "orders.sync": orders,
                "instances.discover": instance_data,
                "instances.submit": instance_data,
                "assembly.ready": readiness,
                "documents.label": documents,
                "documents.cleanup": archive,
            }.get(job.kind)
            if service:
                with queue.keep_alive(job):
                    service.perform(job)
            elif job.kind != "identity.cleanup":
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
    env.add_argument("--native-postgres", action="store_true", help="Отдельная база в системной PostgreSQL")
    env.add_argument("--database-port", type=int, help="Порт PostgreSQL")
    commands.add_parser("migrate")
    commands.add_parser("check")
    owner = commands.add_parser("create-owner")
    owner.add_argument("--login", required=True)
    work = commands.add_parser("worker")
    work.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "generate-env":
            generate_env(args.output, native_postgres=args.native_postgres, database_port=args.database_port)
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
                connections = Connections(
                    db,
                    SecretBox(settings.encryption_key.get_secret_value()),
                    OzonAccessChecker(),
                    OzonReadAdapter,
                    OzonInstanceAdapter,
                    OzonShippingAdapter,
                    OzonLabelAdapter,
                )
                catalog = Catalog(db, connections)
                orders = Orders(db, connections, catalog, lookback_days=settings.orders_lookback_days)
                box = SecretBox(settings.encryption_key.get_secret_value())
                instance_data = InstanceData(
                    db, connections, orders, Marking(box, settings.secret_key.get_secret_value()), box
                )
                documents = Documents(db, connections, orders, box, label_size=settings.ozon_label_size)
                readiness = Readiness(
                    db,
                    connections,
                    orders,
                    instance_data,
                    auto_split=settings.split_orders_on_pack,
                    documents=documents,
                )
                archive = StickerArchive(
                    db,
                    connections,
                    orders,
                    documents,
                    instance_data.marking,
                    instance_data,
                    readiness,
                    settings.secret_key.get_secret_value(),
                )
                worker(
                    db,
                    Identity(db, settings),
                    args.once,
                    catalog,
                    settings.catalog_sync_minutes,
                    orders,
                    settings.orders_sync_minutes,
                    instance_data,
                    readiness,
                    documents,
                    archive,
                )
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
