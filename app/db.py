"""Слой хранения: SQLite, схема и мелкие помощники.

Приложение синхронное (эндпоинты — обычные def, FastAPI выполняет их в пуле
потоков), поэтому на каждый поток заводится собственное соединение.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import settings

log = logging.getLogger("db")

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

-- Кабинет = один магазин на одной площадке. Данные разных кабинетов не
-- пересекаются: у каждой строки есть account_id, а ключи лежат в самом кабинете.
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace TEXT NOT NULL DEFAULT 'ozon',
    title       TEXT NOT NULL,
    client_id   TEXT,
    api_key     TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    sort        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_accounts_active ON accounts(active, sort, id);

CREATE TABLE IF NOT EXISTS postings (
    account_id       INTEGER NOT NULL,
    posting_number   TEXT NOT NULL,
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
    updated_at       TEXT,
    PRIMARY KEY (account_id, posting_number)
);
CREATE INDEX IF NOT EXISTS idx_postings_status ON postings(account_id, status, local_state);
CREATE INDEX IF NOT EXISTS idx_postings_shipment ON postings(account_id, shipment_date);

CREATE TABLE IF NOT EXISTS posting_items (
    account_id     INTEGER NOT NULL,
    posting_number TEXT NOT NULL,
    sku            TEXT NOT NULL,
    offer_id       TEXT,
    name           TEXT,
    quantity       INTEGER NOT NULL DEFAULT 1,
    price          TEXT,
    currency       TEXT,
    mandatory_mark INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, posting_number, sku)
);
CREATE INDEX IF NOT EXISTS idx_items_sku ON posting_items(account_id, sku);
CREATE INDEX IF NOT EXISTS idx_items_offer ON posting_items(account_id, offer_id);

CREATE TABLE IF NOT EXISTS products (
    account_id INTEGER NOT NULL,
    sku        TEXT NOT NULL,
    offer_id   TEXT,
    name       TEXT,
    image      TEXT,
    barcodes   TEXT,
    updated_at TEXT,
    PRIMARY KEY (account_id, sku)
);

CREATE TABLE IF NOT EXISTS product_barcodes (
    account_id INTEGER NOT NULL,
    barcode    TEXT NOT NULL,
    sku        TEXT NOT NULL,
    PRIMARY KEY (account_id, barcode)
);
CREATE INDEX IF NOT EXISTS idx_barcodes_sku ON product_barcodes(account_id, sku);

CREATE TABLE IF NOT EXISTS pack_state (
    user_id        INTEGER PRIMARY KEY,
    account_id     INTEGER,
    posting_number TEXT,
    scanned        TEXT NOT NULL DEFAULT '{}',
    started_at     TEXT,
    updated_at     TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    at             TEXT NOT NULL,
    account_id     INTEGER,
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
    account_id        INTEGER NOT NULL,
    id                TEXT NOT NULL,
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
    updated_at        TEXT,
    PRIMARY KEY (account_id, id)
);
CREATE INDEX IF NOT EXISTS idx_returns_ready ON returns(account_id, is_ready, type);

-- Заказы Авито: структура API другая, поэтому отдельная таблица.
CREATE TABLE IF NOT EXISTS avito_orders (
    account_id      INTEGER NOT NULL,
    id              TEXT NOT NULL,
    marketplace_id  TEXT,
    status          TEXT,
    service_type    TEXT,
    service_name    TEXT,
    dispatch_number TEXT,
    tracking_number TEXT,
    terminal_code   TEXT,
    terminal_address TEXT,
    buyer_name      TEXT,
    buyer_phone     TEXT,
    confirm_till    TEXT,
    ship_till       TEXT,
    delivery_date   TEXT,
    return_status   TEXT,
    return_tracking TEXT,
    price           REAL,
    total           REAL,
    delivery_price  REAL,
    commission      REAL,
    items_count     INTEGER DEFAULT 0,
    positions_count INTEGER DEFAULT 0,
    actions         TEXT,
    created_at_api  TEXT,
    updated_at_api  TEXT,
    raw             TEXT,
    local_state     TEXT NOT NULL DEFAULT 'new',
    confirmed_at    TEXT,
    confirmed_by    TEXT,
    shipped_at      TEXT,
    shipped_by      TEXT,
    printed_at      TEXT,
    print_count     INTEGER NOT NULL DEFAULT 0,
    taken_at        TEXT,
    taken_by        TEXT,
    first_seen_at   TEXT,
    updated_at      TEXT,
    PRIMARY KEY (account_id, id)
);
CREATE INDEX IF NOT EXISTS idx_avito_status ON avito_orders(account_id, status);
CREATE INDEX IF NOT EXISTS idx_avito_return ON avito_orders(account_id, return_status);

CREATE TABLE IF NOT EXISTS avito_order_items (
    account_id INTEGER NOT NULL,
    order_id   TEXT NOT NULL,
    avito_id   TEXT NOT NULL,
    seller_id  TEXT,
    title      TEXT,
    quantity   INTEGER NOT NULL DEFAULT 1,
    price      REAL,
    image      TEXT,
    location   TEXT,
    PRIMARY KEY (account_id, order_id, avito_id)
);

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
    account_id: int | None = None,
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
        "INSERT INTO events(at, account_id, user_id, login, kind, level, posting_number, sku, barcode, message, payload) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    params = (
        now_iso(),
        account_id,
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


def create_sql(table: str) -> str:
    """Оператор CREATE TABLE для таблицы из SCHEMA — нужен при миграции."""
    marker = f"CREATE TABLE IF NOT EXISTS {table} ("
    for statement in SCHEMA.split(";"):
        if marker in statement:
            return statement.strip() + ";"
    raise KeyError(f"в схеме нет таблицы {table}")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


# Таблицы, которые до появления кабинетов хранили данные одного магазина.
ACCOUNT_TABLES = ("postings", "posting_items", "products", "product_barcodes", "returns")


def _schema_columns(table: str) -> list[tuple[str, str]]:
    """Колонки таблицы из SCHEMA: [(имя, остальное определение)]."""
    body = create_sql(table)
    body = body[body.index("(") + 1 : body.rindex(")")]
    parts, depth, current = [], 0, ""
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)

    columns = []
    for part in parts:
        piece = part.strip()
        if not piece or piece.upper().startswith(("PRIMARY KEY", "FOREIGN KEY", "UNIQUE", "CHECK", "--")):
            continue
        name, _, rest = piece.partition(" ")
        columns.append((name, rest.strip()))
    return columns


def _add_missing_columns(conn: sqlite3.Connection, table: str) -> None:
    """Дописать колонки, появившиеся в схеме позже самой таблицы."""
    if not _table_exists(conn, table):
        return
    existing = set(_columns(conn, table))
    for name, definition in _schema_columns(table):
        if name in existing:
            continue
        # NOT NULL без DEFAULT ALTER TABLE не примет — такие колонки требуют
        # перестройки таблицы, а её делаем отдельно и осознанно.
        upper = definition.upper()
        if "NOT NULL" in upper and "DEFAULT" not in upper:
            log.warning("Колонку %s.%s нельзя добавить на месте", table, name)
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        log.info("В таблицу %s добавлена колонка %s", table, name)


def _default_account_id(conn: sqlite3.Connection) -> int:
    """Кабинет по умолчанию: в него попадают данные, накопленные до обновления."""
    row = conn.execute("SELECT id FROM accounts ORDER BY sort, id LIMIT 1").fetchone()
    if row:
        return int(row["id"])
    # Ключи могли лежать в настройках панели (kv) или в .env — переносим в кабинет.
    client_id = api_key = ""
    if _table_exists(conn, "kv"):
        for key, target in (("ozon_client_id", "client_id"), ("ozon_api_key", "api_key")):
            got = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
            value = (got["value"] if got else "") or ""
            if target == "client_id":
                client_id = value.strip()
            else:
                api_key = value.strip()
    if not (client_id and api_key):
        client_id = settings.ozon_client_id
        api_key = settings.ozon_api_key
    cur = conn.execute(
        "INSERT INTO accounts(marketplace, title, client_id, api_key, active, sort, created_at) "
        "VALUES('ozon', ?, ?, ?, 1, 0, ?)",
        ("Ozon", client_id, api_key, now_iso()),
    )
    return int(cur.lastrowid)


def _migrate(conn: sqlite3.Connection) -> None:
    """Привести базу прошлых версий к схеме с кабинетами.

    Порядок важен: индексы новой схемы ссылаются на account_id, поэтому
    таблицы перестраиваются до executescript(SCHEMA).
    """
    if not _table_exists(conn, "accounts"):
        conn.execute(create_sql("accounts"))

    legacy = [t for t in ACCOUNT_TABLES if _table_exists(conn, t) and "account_id" not in _columns(conn, t)]
    account_id = _default_account_id(conn)

    for table in legacy:
        keep = [c for c in _columns(conn, table) if c != "account_id"]
        columns = ", ".join(keep)
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
        # Индексы переезжают вместе со старой таблицей и мешают создать новые.
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ?", (f"{table}_old",)
        ).fetchall():
            if not row["name"].startswith("sqlite_"):
                conn.execute(f'DROP INDEX IF EXISTS "{row["name"]}"')
        conn.execute(create_sql(table))
        conn.execute(
            f"INSERT OR IGNORE INTO {table}(account_id, {columns}) SELECT ?, {columns} FROM {table}_old",
            (account_id,),
        )
        conn.execute(f"DROP TABLE {table}_old")
        log.info("Таблица %s переведена на кабинеты", table)

    # Здесь достаточно добавить колонку: составной ключ не нужен.
    for table in ("pack_state", "events"):
        if _table_exists(conn, table) and "account_id" not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN account_id INTEGER")
            conn.execute(f"UPDATE {table} SET account_id = ?", (account_id,))

    # Новые колонки в уже существующих таблицах (например, возвраты Avito).
    for table in ("avito_orders", "avito_order_items", "postings", "returns", "products"):
        _add_missing_columns(conn, table)


def init_db() -> None:
    conn = connect()
    with _write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _migrate(conn)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    conn.executescript(SCHEMA)
    _seed_admin()


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
