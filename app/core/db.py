"""Слой хранения: SQLite, схема и мелкие помощники.

Приложение синхронное (эндпоинты — обычные def, FastAPI выполняет их в пуле
потоков), поэтому на каждый поток заводится собственное соединение.
"""
from __future__ import annotations

import json
import logging
import re
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
    -- owner | admin | packer. Владелец меняет права и пароли всем, включая
    -- других владельцев; администратор — всем, кроме владельцев.
    role          TEXT NOT NULL DEFAULT 'packer',
    -- Разделы, которые человек видит: список ключей в JSON. Пусто — значит не
    -- настраивали, работает умолчание роли (app/access.py). У владельца поле
    -- не читается: ему доступно всё.
    sections      TEXT,
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

-- Каталог товаров кабинета. Таблица общая: наполняет её та площадка, которая
-- умеет отдавать карточки (Ozon, Яндекс Маркет), а ищут по ней все — сборщик
-- сканирует штрихкод с полки, не зная, чей это товар.
CREATE TABLE IF NOT EXISTS products (
    account_id INTEGER NOT NULL,
    sku        TEXT NOT NULL,
    offer_id   TEXT,
    name       TEXT,
    image      TEXT,
    barcodes   TEXT,
    -- Товар в архиве площадки: он не продаётся, и в разделе «Товары» его быть
    -- не должно. Строку при этом не удаляем — её штрихкоды могут понадобиться,
    -- если архивный товар остался в несобранном заказе.
    archived   INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (account_id, sku)
);
CREATE INDEX IF NOT EXISTS idx_products_live ON products(account_id, archived);

CREATE TABLE IF NOT EXISTS product_barcodes (
    account_id INTEGER NOT NULL,
    barcode    TEXT NOT NULL,
    sku        TEXT NOT NULL,
    PRIMARY KEY (account_id, barcode)
);
CREATE INDEX IF NOT EXISTS idx_barcodes_sku ON product_barcodes(account_id, sku);

-- Сопоставление: карточки разных кабинетов, за которыми стоит один и тот же
-- товар склада. Связь per-карточка, поэтому ключ — (account_id, sku): карточка
-- входит не более чем в одну группу. Главная карточка в группе одна — её
-- название и фото панель показывает в каталоге.
CREATE TABLE IF NOT EXISTS product_links (
    group_id   TEXT NOT NULL,
    account_id INTEGER NOT NULL,
    sku        TEXT NOT NULL,
    is_main    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    created_by TEXT,
    PRIMARY KEY (account_id, sku)
);
CREATE INDEX IF NOT EXISTS idx_product_links_group ON product_links(group_id);

-- Артикулы, которые панель предлагать больше не должна: одинаковый артикул на
-- двух площадках не всегда один товар, и «не сопоставлять» — это ответ, а не
-- откладывание. Кабинета здесь нет: решение принимается про артикул целиком.
CREATE TABLE IF NOT EXISTS product_link_skips (
    article    TEXT PRIMARY KEY,
    created_at TEXT,
    created_by TEXT
);

CREATE TABLE IF NOT EXISTS product_sets (
    account_id INTEGER NOT NULL,
    sku        TEXT NOT NULL,
    title      TEXT,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT,
    created_by TEXT,
    updated_at TEXT,
    PRIMARY KEY (account_id, sku)
);

-- Часть набора. Может быть товаром площадки (part_sku) или просто штрихкодом,
-- если такого товара в каталоге нет: набор собирают из того, что есть на
-- складе, и не всё из этого продаётся отдельно.
CREATE TABLE IF NOT EXISTS product_set_items (
    account_id INTEGER NOT NULL,
    set_sku    TEXT NOT NULL,
    -- Ключ части внутри набора: SKU товара либо 'bc:<штрихкод>'. По нему
    -- считается прогресс сборки, поэтому он обязан быть устойчивым.
    part_key   TEXT NOT NULL,
    part_sku   TEXT,
    barcode    TEXT,
    title      TEXT,
    quantity   INTEGER NOT NULL DEFAULT 1,
    sort       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, set_sku, part_key)
);
CREATE INDEX IF NOT EXISTS idx_set_items_sku ON product_set_items(account_id, part_sku);
CREATE INDEX IF NOT EXISTS idx_set_items_barcode ON product_set_items(account_id, barcode);

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

CREATE TABLE IF NOT EXISTS return_acts (
    id           TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    created_by   TEXT,
    -- byday    — полученные за указанное число свёл в акт человек;
    -- nosheet  — возврат пропал из выдачи, а «Получен» по нему не приходил;
    -- received | ozon | upload | account | all — акты прежних версий.
    kind         TEXT NOT NULL DEFAULT 'account',
    account_id   INTEGER,
    -- Число, за которое собран акт (для kind = byday): местная дата получения.
    received_day TEXT,
    -- Номер акта внутри этого числа. За возвратами ездят несколько раз в день,
    -- и два акта, составленные в одну минуту, иначе неотличимы в списке.
    day_seq      INTEGER,
    -- Колонки актов выдачи Ozon. Оставлены ради баз прежних версий: акты о
    -- возвратах площадка не отдаёт, и новые акты их не заполняют.
    giveout_id   TEXT,
    giveout_status TEXT,
    confirmed_at TEXT,
    confirmed_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_return_acts_open ON return_acts(confirmed_at, created_at);
CREATE INDEX IF NOT EXISTS idx_return_acts_day ON return_acts(kind, account_id, received_day);

-- Заказы Авито: структура API другая, поэтому отдельная таблица.
CREATE TABLE IF NOT EXISTS shipped_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     INTEGER NOT NULL,
    marketplace    TEXT NOT NULL DEFAULT 'ozon',
    posting_number TEXT NOT NULL DEFAULT '',
    -- Ключ единицы товара: sku, либо bc:<штрихкод>, если товар не опознан.
    item_key       TEXT NOT NULL,
    sku            TEXT,
    offer_id       TEXT,
    name           TEXT,
    barcode        TEXT,
    -- Порядковый номер единицы внутри отправления: 1..количество.
    unit_no        INTEGER NOT NULL DEFAULT 1,
    -- ok — пара сошлась; unmatched — штрихкода нет в справочнике (Avito);
    -- error — пересорт: не тот товар или не то отправление.
    status         TEXT NOT NULL DEFAULT 'ok',
    reason         TEXT,
    user_id        INTEGER,
    login          TEXT,
    scanned_at     TEXT NOT NULL,
    report_date    TEXT NOT NULL
);
-- Защита от дублей: одна и та же единица товара в одном отправлении
-- записывается один раз, сколько бы раз её ни отсканировали.
CREATE UNIQUE INDEX IF NOT EXISTS idx_shipped_unit
    ON shipped_items(account_id, posting_number, item_key, unit_no)
    WHERE status <> 'error';
CREATE INDEX IF NOT EXISTS idx_shipped_day ON shipped_items(report_date, account_id, status);
CREATE INDEX IF NOT EXISTS idx_shipped_posting ON shipped_items(account_id, posting_number);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _restrict_access(db_path: str) -> None:
    """Права 0600 на базу и её спутников.

    В базе лежат ключи Ozon и Avito открытым текстом и хеши паролей, а sqlite
    создаёт файл по umask — обычно доступным на чтение любому пользователю
    сервера. Права выставляем на каждом подключении: так подтягиваются и базы,
    заведённые прежними версиями. Так же защищён data/.secret_key в config.py.

    Владеть файлом может другой пользователь (панель запускали то от root, то от
    служебного) — тогда chmod не пройдёт, и это не повод падать: панель работает,
    а о правах пишем в журнал.
    """
    for path in (db_path, f"{db_path}-wal", f"{db_path}-shm"):
        try:
            Path(path).chmod(0o600)
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.warning("Не удалось закрыть доступ к %s: %s", path, exc)


def _lower_ru(value: Any) -> Any:
    """Регистр по-русски. Не строка — возвращаем как есть, чтобы не съесть NULL."""
    return value.lower() if isinstance(value, str) else value


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
        # LIKE в SQLite не различает регистр только у латиницы: «кофе» не нашло
        # бы «Кофе», а панель русскоязычная, и в поиске это первое, что
        # пробуют. Своя функция приводит регистр средствами Python — он знает
        # про кириллицу. Индексу это не мешает: поиск по каталогу и так идёт
        # перебором с LIKE.
        conn.create_function("lower_ru", 1, _lower_ru, deterministic=True)
        # После journal_mode=WAL рядом появляются -wal и -shm: закрываем и их.
        _restrict_access(settings.db_path)
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


def json_list(raw: Any) -> list:
    """Колонка со списком в JSON — списком. Мусор в колонке не должен ронять страницу."""
    if isinstance(raw, list):
        return raw
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


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


def _without_comments(sql: str) -> str:
    """Убрать «-- ...» из SQL.

    Схему мы разбираем сами — делим по «;» и по запятым. Комментарий по-русски
    почти наверняка содержит и то и другое, поэтому разбирать текст с
    комментариями нельзя: оператор разрывается посреди строки.
    """
    return re.sub(r"--[^\n]*", "", sql)


def _registry():
    """Реестр площадок — лениво: их пакеты сами импортируют этот модуль."""
    from ..markets import registry

    return registry


def full_schema() -> str:
    """Схема целиком: общие таблицы плюс таблицы каждой площадки."""
    return SCHEMA + "".join(market.schema for market in _registry().all_markets())


def create_sql(table: str) -> str:
    """Оператор CREATE TABLE для таблицы из схемы — нужен при миграции."""
    marker = f"CREATE TABLE IF NOT EXISTS {table} ("
    for statement in _without_comments(full_schema()).split(";"):
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

# Таблицам отсюда дописываются колонки, появившиеся в схеме позже их самих.
# Оператор создания таблицы колонку в живую таблицу не добавляет, поэтому
# каждая таблица схемы, которая может уже существовать на сервере, обязана
# быть в списке: иначе обновление падает на первом запросе к новой колонке.
# Полноту списка держит проверка в tests/test_migration.py.
CORE_TABLES_WITH_NEW_COLUMNS = (
    "accounts", "users", "kv", "events", "pack_state",
    "products", "product_barcodes", "product_links", "product_link_skips",
    "product_sets", "product_set_items", "return_acts", "shipped_items",
)


def tables_with_new_columns() -> tuple[str, ...]:
    """Общие таблицы плюс таблицы всех площадок — им дописываются новые колонки."""
    return CORE_TABLES_WITH_NEW_COLUMNS + tuple(
        table for market in _registry().all_markets() for table in market.tables
    )


def _schema_columns(table: str) -> list[tuple[str, str]]:
    """Колонки таблицы из SCHEMA: [(имя, остальное определение)].

    Комментарии вырезаем до разбора: запятая внутри «-- ...» иначе рвёт строку
    пополам, и в ALTER TABLE уезжает кусок русского текста вместо колонки.
    """
    body = _without_comments(create_sql(table))
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
        # Переносим только те колонки, что есть и в новой схеме: какие-то могли
        # исчезнуть (например, локальная отметка «забрали»).
        target = {name for name, _definition in _schema_columns(table)}
        keep = [c for c in _columns(conn, table) if c != "account_id" and c in target]
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

    for table in tables_with_new_columns():
        _add_missing_columns(conn, table)


# Таблицы с данными кабинета: чистятся вместе с ним. product_link_skips сюда
# не входит — в ней решение про артикул, а не про кабинет.
CORE_DATA_TABLES = ("products", "product_barcodes", "product_links",
                    "product_sets", "product_set_items")


def data_tables() -> tuple[str, ...]:
    """Все таблицы с данными кабинетов — общие и площадок."""
    return CORE_DATA_TABLES + tuple(table for market in _registry().all_markets() for table in market.tables)
KV_GENERATED_CLEANED = "generated_data_cleaned"
KV_CONTACTS_CLEANED = "buyer_contacts_cleaned"
KV_OWNER_SET = "owner_role_assigned"


def _parsed_moment(value: Any) -> str | None:
    """Момент времени или None — строку «как пришла» здесь принимать нельзя."""
    if not value:
        return None
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return str(value)
def raw_tables() -> tuple[str, ...]:
    """Таблицы, у которых есть колонка raw с ответом площадки целиком."""
    return tuple(table for market in _registry().all_markets() for table in market.raw_tables)


def _ensure_owner(conn: sqlite3.Connection) -> None:
    """Назначить владельца, если его ещё нет.

    До 1.21 ролей было две, и «сменить пароль владельцу» или «выдать доступ»
    было некому: после обновления в базе одни администраторы. Владельцем
    становится самый первый администратор — тот, кого завёл установщик.

    Один раз: дальше роли меняет человек, и понижение владельца обратно в
    администраторы не должно откатываться при каждом перезапуске.
    """
    if not _table_exists(conn, "users"):
        return
    done = conn.execute("SELECT value FROM kv WHERE key = ?", (KV_OWNER_SET,)).fetchone()
    if done:
        return
    if not conn.execute("SELECT id FROM users WHERE role = 'owner' LIMIT 1").fetchone():
        first = conn.execute(
            "SELECT id, login FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
        ).fetchone()
        if first:
            conn.execute("UPDATE users SET role = 'owner' WHERE id = ?", (first["id"],))
            log.info("Владельцем панели назначен %s", first["login"])
        elif conn.execute("SELECT id FROM users LIMIT 1").fetchone():
            # Администраторов в базе нет вовсе — назначать владельца наугад
            # нельзя, это выдача полных прав. Панель и так была без управления.
            log.warning("В базе нет администраторов: владельца назначить некому")
    conn.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (KV_OWNER_SET, now_iso()),
    )


def _drop_generated_data(conn: sqlite3.Connection) -> None:
    """Убрать данные, оставшиеся от демо-режима.

    До версии 1.3 панель без ключей показывала сгенерированные заказы, товары и
    возвраты. Демо-режима больше нет, но записи могли осесть в базе. У кабинета
    без ключей ничего настоящего быть не может, поэтому его данные удаляются —
    один раз, чтобы случайно снятые ключи не стирали рабочую историю.
    """
    done = conn.execute("SELECT value FROM kv WHERE key = ?", (KV_GENERATED_CLEANED,)).fetchone()
    if done:
        return
    removed = 0
    for row in conn.execute("SELECT id, marketplace, client_id, api_key FROM accounts").fetchall():
        has_keys = bool((row["client_id"] or "").strip() and (row["api_key"] or "").strip())
        if not has_keys and row["marketplace"] == "ozon":
            # Первый кабинет Ozon мог работать на ключах из .env — они настоящие.
            has_keys = bool(settings.ozon_client_id and settings.ozon_api_key)
        if has_keys:
            continue
        for table in data_tables():
            if _table_exists(conn, table):
                removed += conn.execute(f"DELETE FROM {table} WHERE account_id = ?", (row["id"],)).rowcount or 0
    conn.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (KV_GENERATED_CLEANED, now_iso()),
    )
    if removed:
        log.warning("Удалено %s записей, сгенерированных прежним демо-режимом", removed)


def _encrypt_account_keys(conn: sqlite3.Connection) -> None:
    """Зашифровать ключи площадок, лежащие в базе открытым текстом.

    Идёт при каждом запуске и трогает только незашифрованные значения, поэтому
    безопасна и на свежей базе, и на той, что обновлялась в несколько заходов.
    """
    from . import crypto

    if not _table_exists(conn, "accounts"):
        return
    updated = 0
    for row in conn.execute("SELECT id, api_key FROM accounts").fetchall():
        value = (row["api_key"] or "").strip()
        if not value or crypto.is_encrypted(value):
            continue
        conn.execute(
            "UPDATE accounts SET api_key = ? WHERE id = ?", (crypto.encrypt(value), row["id"])
        )
        updated += 1
    if updated:
        log.info("Ключи площадок зашифрованы: %s кабинет(ов)", updated)


def _drop_buyer_contacts(conn: sqlite3.Connection) -> None:
    """Разово вычистить контакты покупателей, накопленные прежними версиями.

    Телефон покупателя панель больше не сохраняет, а в колонках raw контакты
    вырезаются перед записью (store.without_contacts). Записи, сделанные до
    обновления, чистим один раз здесь: иначе телефоны так и лежали бы в базе,
    хотя ни один экран их не показывает.
    """
    from .store import without_contacts

    done = conn.execute("SELECT value FROM kv WHERE key = ?", (KV_CONTACTS_CLEANED,)).fetchone()
    if done:
        return

    cleaned = 0
    if _table_exists(conn, "avito_orders") and "buyer_phone" in _columns(conn, "avito_orders"):
        # Колонку не удаляем: DROP COLUMN есть не во всех сборках sqlite, а
        # пустая неиспользуемая колонка безвредна. Значения — стираем.
        cleaned += conn.execute(
            "UPDATE avito_orders SET buyer_phone = NULL WHERE buyer_phone IS NOT NULL"
        ).rowcount or 0

    for table in raw_tables():
        if not _table_exists(conn, table) or "raw" not in _columns(conn, table):
            continue
        key = "posting_number" if table == "postings" else "id"
        for row in conn.execute(f"SELECT account_id, {key} AS row_key, raw FROM {table}").fetchall():
            if not row["raw"]:
                continue
            try:
                parsed = json.loads(row["raw"])
            except ValueError:
                continue
            scrubbed = json.dumps(without_contacts(parsed), ensure_ascii=False)
            if scrubbed != row["raw"]:
                conn.execute(
                    f"UPDATE {table} SET raw = ? WHERE account_id = ? AND {key} = ?",
                    (scrubbed, row["account_id"], row["row_key"]),
                )
                cleaned += 1

    conn.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (KV_CONTACTS_CLEANED, now_iso()),
    )
    if cleaned:
        log.info("Из базы убраны контакты покупателей: %s записей", cleaned)


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
    conn.executescript(full_schema())
    with _write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _drop_generated_data(conn)
            _drop_buyer_contacts(conn)
            # Разовые правки своих таблиц — у каждой площадки свои, в её порядке.
            for market in _registry().all_markets():
                if market.migrate:
                    market.migrate(conn)
            _ensure_owner(conn)
            _encrypt_account_keys(conn)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    _seed_admin()


def _seed_admin() -> None:
    from .security import hash_password, random_password

    row = query_one("SELECT COUNT(*) AS c FROM users")
    if row and row["c"]:
        return
    password = settings.admin_password or random_password()
    # Первая учётка панели — владелец: тот, кто её поставил. Иначе на свежей
    # установке владельца нет вовсе, и некому ни менять пароли, ни назначать
    # владельцев — то есть половина прав недоступна никому.
    execute(
        "INSERT INTO users(login, password_hash, role, active, created_at) VALUES(?, ?, 'owner', 1, ?)",
        (settings.admin_login, hash_password(password), now_iso()),
    )
    if not settings.admin_password:
        print(
            "\n[ozon-pack] Создан администратор: "
            f"логин={settings.admin_login} пароль={password}\n"
            "Сохраните пароль — он показывается один раз (или задайте ADMIN_PASSWORD в .env).\n",
            flush=True,
        )
