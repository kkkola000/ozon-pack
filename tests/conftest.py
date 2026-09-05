"""Общие фикстуры: изолированная БД и демо-клиент Ozon на каждый тест."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import accounts, avito, db, ozon, store  # noqa: E402
from app.config import settings  # noqa: E402


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "demo_forced", True)
    monkeypatch.setattr(settings, "require_all_items", True)
    monkeypatch.setattr(settings, "autoprint", True)
    monkeypatch.setattr(settings, "auto_ship_on_scan", False)
    monkeypatch.setattr(settings, "admin_password", "test-admin-pass")
    # Фоновый поток синхронизации в тестах не нужен: данные грузим фикстурой явно.
    monkeypatch.setattr(settings, "sync_enabled", False)
    db._local.conn = None
    ozon.reset_client()
    avito.reset_client()
    db.init_db()
    yield
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
    db._local.conn = None
    ozon.reset_client()
    avito.reset_client()


@pytest.fixture
def account():
    """Кабинет по умолчанию — его создаёт init_db()."""
    return accounts.default_account()


@pytest.fixture
def avito_account():
    return accounts.get(accounts.create("avito", "Avito демо"))


@pytest.fixture
def demo_data():
    """Загрузить демо-отправления, товары и возвраты в тестовую БД."""
    from app import sync

    sync.sync_all()
    return ozon.get_client()


@pytest.fixture
def user():
    row = db.query_one("SELECT id, login, role FROM users WHERE login = 'admin'")
    return dict(row)


@pytest.fixture
def other_user():
    from app.security import hash_password

    db.execute(
        "INSERT INTO users(login, password_hash, role, active, created_at) VALUES('petrov', ?, 'packer', 1, ?)",
        (hash_password("secret123"), db.now_iso()),
    )
    row = db.query_one("SELECT id, login, role FROM users WHERE login = 'petrov'")
    return dict(row)


def account_id() -> int:
    return accounts.default_account()["id"]


def pick_posting(status=store.STATUS_AWAITING_DELIVER, positions=None):
    """Первое отправление в статусе, при желании — с нужным числом позиций."""
    sql = "SELECT * FROM postings WHERE account_id = ? AND status = ? AND local_state = 'new'"
    params = [account_id(), status]
    if positions:
        sql += " AND positions_count = ?"
        params.append(positions)
    row = db.query_one(sql + " ORDER BY posting_number", params)
    assert row is not None, f"нет отправления {status} с {positions} позициями"
    return store.posting_view(row)


def barcode_of(sku: str) -> str:
    row = db.query_one(
        "SELECT barcode FROM product_barcodes WHERE account_id = ? AND sku = ?", (account_id(), sku)
    )
    assert row is not None, f"нет штрихкода для SKU {sku}"
    return row["barcode"]
