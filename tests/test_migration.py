"""Обновление базы прошлой версии до схемы с кабинетами.

На сервере уже лежит база без account_id: собранные отправления, товары,
возвраты и журнал. Обновление панели не должно ничего из этого потерять.
"""
import sqlite3
import subprocess

import pytest

from app import accounts, db, ozon
from app.config import BASE_DIR, settings

# Схему прошлой версии берём из истории git, а не переписываем руками:
# так тест проверяет реальную базу пользователя, а не наше представление о ней.
LEGACY_COMMIT = "0410a34"


def legacy_schema() -> str:
    try:
        source = subprocess.run(
            ["git", "-C", str(BASE_DIR), "show", f"{LEGACY_COMMIT}:app/db.py"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        # В распакованном архиве истории нет — проверять нечего, но и падать незачем.
        pytest.skip(f"нет истории git для схемы {LEGACY_COMMIT}: {exc}")
    start = source.index('SCHEMA = """') + len('SCHEMA = """')
    end = source.index('"""', start)
    return source[start:end]


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    """База предыдущей версии с данными, ещё без кабинетов."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(legacy_schema())
    conn.execute(
        "INSERT INTO users(login, password_hash, role, active, created_at) VALUES('admin', 'x', 'admin', 1, '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO postings(posting_number, order_number, status, local_state, packed_by, packed_at,"
        " items_count, positions_count, first_seen_at, updated_at)"
        " VALUES('111-222-1', '111-222', 'awaiting_deliver', 'packed', 'ivanov', '2026-01-02', 2, 1,"
        " '2026-01-01', '2026-01-02')"
    )
    conn.execute(
        "INSERT INTO posting_items(posting_number, sku, offer_id, name, quantity)"
        " VALUES('111-222-1', '555', 'ART-1', 'Кофе', 2)"
    )
    conn.execute(
        "INSERT INTO products(sku, offer_id, name, barcodes, updated_at)"
        " VALUES('555', 'ART-1', 'Кофе', '[\"4600000000017\"]', '2026-01-01')"
    )
    conn.execute("INSERT INTO product_barcodes(barcode, sku) VALUES('4600000000017', '555')")
    conn.execute(
        "INSERT INTO returns(id, type, status_sys, product_name, quantity, is_ready, first_seen_at, updated_at)"
        " VALUES('r-1', 'FBS', 'ArrivedAtReturnPlace', 'Чайник', 1, 1, '2026-01-01', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO events(at, kind, level, posting_number, message) "
        "VALUES('2026-01-02', 'pack_complete', 'info', '111-222-1', 'Отправление собрано')"
    )
    conn.execute("INSERT INTO pack_state(user_id, posting_number, scanned) VALUES(1, '111-222-1', '{}')")
    conn.execute("INSERT INTO kv(key, value) VALUES('ozon_client_id', '123456')")
    conn.execute("INSERT INTO kv(key, value) VALUES('ozon_api_key', 'secret-key')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(settings, "db_path", str(path))
    db._local.conn = None
    ozon.reset_client()
    db.init_db()
    yield path
    existing = getattr(db._local, "conn", None)
    if existing:
        existing.close()
    db._local.conn = None


def test_data_survives_upgrade(legacy_db):
    posting = db.query_one("SELECT * FROM postings WHERE posting_number = '111-222-1'")
    assert posting is not None
    assert posting["local_state"] == "packed", "отметка сборки должна сохраниться"
    assert posting["packed_by"] == "ivanov"

    assert db.query_one("SELECT COUNT(*) AS c FROM posting_items")["c"] == 1
    assert db.query_one("SELECT COUNT(*) AS c FROM products")["c"] == 1
    assert db.query_one("SELECT COUNT(*) AS c FROM product_barcodes")["c"] == 1
    assert db.query_one("SELECT COUNT(*) AS c FROM returns")["c"] == 1
    assert db.query_one("SELECT COUNT(*) AS c FROM events")["c"] == 1
    assert db.query_one("SELECT COUNT(*) AS c FROM users")["c"] == 1


def test_everything_lands_in_one_cabinet(legacy_db):
    account = accounts.default_account()
    assert account is not None
    assert account["marketplace"] == "ozon"
    for table in ("postings", "posting_items", "products", "product_barcodes", "returns"):
        rows = db.query(f"SELECT DISTINCT account_id FROM {table}")
        assert [row["account_id"] for row in rows] == [account["id"]], table


def test_keys_move_from_kv_into_cabinet(legacy_db, monkeypatch):
    """Ключи, введённые в прошлой версии, продолжают работать после обновления."""
    monkeypatch.setattr(settings, "demo_forced", False)
    account = accounts.default_account()
    assert accounts.credentials(account) == ("123456", "secret-key", "panel")
    assert not accounts.is_demo(account)


def test_old_pack_state_belongs_to_cabinet(legacy_db):
    row = db.query_one("SELECT * FROM pack_state WHERE user_id = 1")
    assert row["account_id"] == accounts.default_account()["id"]


def test_second_upgrade_is_a_no_op(legacy_db):
    """Повторный запуск панели не должен ничего ломать или дублировать."""
    before = accounts.all_accounts()
    db.init_db()
    assert len(accounts.all_accounts()) == len(before)
    assert db.query_one("SELECT COUNT(*) AS c FROM postings")["c"] == 1


def test_legacy_tables_are_gone(legacy_db):
    left = db.query(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '%_old'"
    )
    assert not left, f"остались временные таблицы миграции: {[r['name'] for r in left]}"
