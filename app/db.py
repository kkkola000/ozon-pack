"""Слой хранения: SQLite, схема и мелкие помощники.

Приложение синхронное (эндпоинты — обычные def, FastAPI выполняет их в пуле
потоков), поэтому на каждый поток заводится собственное соединение.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import settings

_local = threading.local()
_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    login         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'packer',
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS postings (
    posting_number   TEXT PRIMARY KEY,
    order_id         INTEGER,
    order_number     TEXT,
    status           TEXT,
    substatus        TEXT,
    in_process_at    TEXT,
    shipment_date    TEXT,
    delivering_date  TEXT,
    delivery_method  TEXT,
    warehouse_id     INTEGER,
    warehouse_name   TEXT,
    tpl_provider     TEXT,
    tracking_number  TEXT,
    is_express       INTEGER DEFAULT 0,
    is_multibox      INTEGER DEFAULT 0,
    multi_box_qty    INTEGER DEFAULT 0,
    barcode_upper    TEXT,
    barcode_lower    TEXT,
    region           TEXT,
    city             TEXT,
    delivery_type    TEXT,
    payment_type     TEXT,
    is_premium       INTEGER DEFAULT 0,
    requires_mark    INTEGER DEFAULT 0,
    requires_gtd     INTEGER DEFAULT 0,
    items_count      INTEGER DEFAULT 0,
    positions_count  INTEGER DEFAULT 0,
    cancel_reason    TEXT,
    raw              TEXT,
    local_state      TEXT NOT NULL DEFAULT 'new',
    claim_user_id    INTEGER,
    claim_login      TEXT,
    claim_at         TEXT,
    printed_at       TEXT,
    print_count      INTEGER NOT NULL DEFAULT 0,
    packed_at        TEXT,
    packed_by        TEXT,
    shipped_at       TEXT,
    note             TEXT,
    first_seen_at    TEXT,
    updated_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_postings_status ON postings(status, local_state);
CREATE INDEX IF NOT EXISTS idx_postings_shipment ON postings(shipment_date);

CREATE TABLE IF NOT EXISTS posting_items (
    posting_number TEXT NOT NULL,
    sku            TEXT NOT NULL,
    offer_id       TEXT,
    name           TEXT,
    quantity       INTEGER NOT NULL DEFAULT 1,
    price          TEXT,
    currency       TEXT,
    mandatory_mark INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (posting_number, sku)
);
CREATE INDEX IF NOT EXISTS idx_items_sku ON posting_items(sku);
CREATE INDEX IF NOT EXISTS idx_items_offer ON posting_items(offer_id);

CREATE TABLE IF NOT EXISTS products (
    sku        TEXT PRIMARY KEY,
    offer_id   TEXT,
    name       TEXT,
    image      TEXT,
    barcodes   TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS product_barcodes (
    barcode TEXT PRIMARY KEY,
    sku     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_barcodes_sku ON product_barcodes(sku);

CREATE TABLE IF NOT EXISTS pack_state (
    user_id        INTEGER PRIMARY KEY,
    posting_number TEXT,
    scanned        TEXT NOT NULL DEFAULT '{}',
    started_at     TEXT,
    updated_at     TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    at             TEXT NOT NULL,
    user_id        INTEGER,
    login          TEXT,
    kind           TEXT NOT NULL,
    level          TEXT NOT NULL DEFAULT 'info',
    posting_number TEXT,
    sku            TEXT,
    barcode        TEXT,
    message        TEXT,
    payload        TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_at ON events(at DESC);
CREATE INDEX IF NOT EXISTS idx_events_posting ON events(posting_number);

CREATE TABLE IF NOT EXISTS returns (
    id                TEXT PRIMARY KEY,
    type              TEXT,
    scheme            TEXT,
    status_sys        TEXT,
    status_name       TEXT,
    order_id          INTEGER,
    order_number      TEXT,
    posting_number    TEXT,
    sku               TEXT,
    offer_id          TEXT,
    product_name      TEXT,
    quantity          INTEGER DEFAULT 1,
    price             TEXT,
    currency          TEXT,
    place_name        TEXT,
    place_address     TEXT,
    target_place_name TEXT,
    return_reason     TEXT,
    return_date       TEXT,
    final_moment      TEXT,
    storage_until     TEXT,
    storage_sum       TEXT,
    barcode           TEXT,
    is_ready          INTEGER NOT NULL DEFAULT 0,
    raw               TEXT,
    taken_at          TEXT,
    taken_by          TEXT,
    printed_at        TEXT,
    first_seen_at     TEXT,
    updated_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_returns_ready ON returns(is_ready, type);

CREATE TABLE IF NOT EXISTS print_jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL DEFAULT 'label',
    posting_number TEXT,
    title          TEXT,
    payload        BLOB NOT NULL,
    status         TEXT NOT NULL DEFAULT 'queued',
    attempts       INTEGER NOT NULL DEFAULT 0,
    error          TEXT,
    created_by     TEXT,
    created_at     TEXT NOT NULL,
    taken_at       TEXT,
    done_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_print_jobs_status ON print_jobs(status, id);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(settings.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


@contextmanager
def write() -> Iterator[sqlite3.Connection]:
    """Транзакция на запись: sqlite не любит параллельных писателей."""
    conn = connect()
    with _write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, tuple(params)).fetchall()


def query_one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return connect().execute(sql, tuple(params)).fetchone()


def execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    with write() as conn:
        return conn.execute(sql, tuple(params))


def kv_get(key: str, default: str | None = None) -> str | None:
    row = query_one("SELECT value FROM kv WHERE key = ?", (key,))
    return row["value"] if row else default


def kv_set(key: str, value: str) -> None:
    execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def log_event(
    kind: str,
    *,
    level: str = "info",
    user: dict | None = None,
    posting_number: str | None = None,
    sku: str | None = None,
    barcode: str | None = None,
    message: str | None = None,
    payload: Any = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Журнал действий — источник правды при разборе пересорта."""
    sql = (
        "INSERT INTO events(at, user_id, login, kind, level, posting_number, sku, barcode, message, payload) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    params = (
        now_iso(),
        (user or {}).get("id"),
        (user or {}).get("login"),
        kind,
        level,
        posting_number,
        sku,
        barcode,
        message,
        json.dumps(payload, ensure_ascii=False) if payload is not None else None,
    )
    if conn is not None:
        conn.execute(sql, params)
    else:
        execute(sql, params)


def init_db() -> None:
    conn = connect()
    conn.executescript(SCHEMA)
    _seed_admin()
    _seed_print_agent_token()


def _seed_admin() -> None:
    from .security import hash_password, random_password

    row = query_one("SELECT COUNT(*) AS c FROM users")
    if row and row["c"]:
        return
    password = settings.admin_password or random_password()
    execute(
        "INSERT INTO users(login, password_hash, role, active, created_at) VALUES(?, ?, 'admin', 1, ?)",
        (settings.admin_login, hash_password(password), now_iso()),
    )
    if not settings.admin_password:
        print(
            "\n[ozon-pack] Создан администратор: "
            f"логин={settings.admin_login} пароль={password}\n"
            "Сохраните пароль — он показывается один раз (или задайте ADMIN_PASSWORD в .env).\n",
            flush=True,
        )


def _seed_print_agent_token() -> None:
    """Ключ агента печати нужен ещё до того, как кто-то откроет настройки."""
    from .options import get_agent_token

    get_agent_token(create=True)
