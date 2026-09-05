"""Кабинеты: ключи площадок, демо-режим и разделение данных."""
import pytest

from app import accounts, avito, db, ozon, store, sync
from app.config import settings


@pytest.fixture
def ozon_account():
    return accounts.default_account()


def test_default_account_created_on_init(ozon_account):
    assert ozon_account is not None
    assert ozon_account["marketplace"] == "ozon"
    assert ozon_account["active"] == 1


def test_demo_without_keys(monkeypatch, ozon_account):
    monkeypatch.setattr(settings, "demo_forced", False)
    monkeypatch.setattr(settings, "ozon_client_id", "")
    monkeypatch.setattr(settings, "ozon_api_key", "")
    assert accounts.is_demo(ozon_account)
    assert accounts.credentials(ozon_account)[2] == "none"
    assert isinstance(ozon.get_client(ozon_account), ozon.DemoOzonClient)


def test_panel_keys_win_over_env(monkeypatch, ozon_account):
    monkeypatch.setattr(settings, "demo_forced", False)
    monkeypatch.setattr(settings, "ozon_client_id", "env-id")
    monkeypatch.setattr(settings, "ozon_api_key", "env-key")
    assert accounts.credentials(ozon_account) == ("env-id", "env-key", "env")

    accounts.update(ozon_account["id"], client_id="panel-id", api_key="panel-key")
    updated = accounts.get(ozon_account["id"])
    assert accounts.credentials(updated) == ("panel-id", "panel-key", "panel")
    assert not accounts.is_demo(updated)
    client = ozon.get_client(updated)
    assert not isinstance(client, ozon.DemoOzonClient)
    assert client.client_id == "panel-id"


def test_clear_keys_returns_to_env_then_demo(monkeypatch, ozon_account):
    monkeypatch.setattr(settings, "demo_forced", False)
    monkeypatch.setattr(settings, "ozon_client_id", "env-id")
    monkeypatch.setattr(settings, "ozon_api_key", "env-key")
    accounts.update(ozon_account["id"], client_id="panel-id", api_key="panel-key")

    accounts.update(ozon_account["id"], client_id="", api_key="")
    assert accounts.credentials(accounts.get(ozon_account["id"]))[2] == "env"

    monkeypatch.setattr(settings, "ozon_client_id", "")
    monkeypatch.setattr(settings, "ozon_api_key", "")
    assert accounts.is_demo(accounts.get(ozon_account["id"]))


def test_forced_demo_ignores_keys(monkeypatch, ozon_account):
    monkeypatch.setattr(settings, "demo_forced", True)
    accounts.update(ozon_account["id"], client_id="panel-id", api_key="panel-key")
    account = accounts.get(ozon_account["id"])
    assert accounts.is_demo(account), "OZON_DEMO=1 должен перекрывать любые ключи"
    assert isinstance(ozon.get_client(account), ozon.DemoOzonClient)


def test_key_is_masked():
    assert accounts.mask("abcdefghij").endswith("ghij")
    assert "abcdef" not in accounts.mask("abcdefghij")
    assert accounts.mask("") == ""


def test_saving_keys_is_logged(ozon_account):
    accounts.update(ozon_account["id"], client_id="id-1", api_key="key-1", user={"id": 1, "login": "admin"})
    row = db.query_one("SELECT * FROM events WHERE kind = 'account_credentials_set' ORDER BY id DESC")
    assert row is not None
    assert "key-1" not in (row["message"] or ""), "секрет не должен попадать в журнал"


@pytest.mark.parametrize(
    "client_id,api_key",
    [
        ("123456", ""),
        ("кириллица", "ключ"),          # неверная раскладка
        ("123456", "ключ-по-русски"),
        ("123456", "с пробелом внутри"),
        ("1" * 300, "x" * 300),
    ],
)
def test_invalid_credentials_rejected(client_id, api_key):
    assert accounts.validate("ozon", "Магазин", client_id, api_key) is not None


def test_valid_credentials_accepted():
    assert accounts.validate("ozon", "Магазин", "123456", "a1b2c3d4-e5f6-7890-abcd-ef1234567890") is None
    assert accounts.validate("avito", "Магазин", "abc123", "secret-value") is None


def test_title_is_required():
    assert accounts.validate("ozon", "  ", "123456", "abcdef") is not None


def test_unknown_marketplace_rejected():
    assert accounts.validate("wildberries", "Магазин", "1", "2") is not None


# ------------------------------------------------------------------ изоляция кабинетов
def test_two_cabinets_keep_data_apart(demo_data):
    """Главное свойство кабинетов: товар одного магазина не виден в другом."""
    first = accounts.default_account()
    second_id = accounts.create("ozon", "Второй магазин")
    second = accounts.get(second_id)
    sync.sync_account(second)

    first_numbers = {row["posting_number"] for row in db.query(
        "SELECT posting_number FROM postings WHERE account_id = ?", (first["id"],))}
    second_numbers = {row["posting_number"] for row in db.query(
        "SELECT posting_number FROM postings WHERE account_id = ?", (second_id,))}
    assert first_numbers and second_numbers
    assert not (first_numbers & second_numbers), "демо-кабинеты должны выдавать разные отправления"

    # Штрихкод из второго кабинета не должен опознаваться в первом.
    barcode = db.query_one(
        "SELECT barcode FROM product_barcodes WHERE account_id = ? LIMIT 1", (second_id,)
    )["barcode"]
    from app import packing

    kind, _target = packing.classify(second_id, barcode)
    assert kind == "product"
    kind_other, _ = packing.classify(first["id"], barcode)
    assert kind_other != "product" or db.query_one(
        "SELECT 1 FROM product_barcodes WHERE account_id = ? AND barcode = ?", (first["id"], barcode)
    ), "чужой штрихкод не должен считаться товаром кабинета"


def test_deleting_cabinet_removes_its_data(demo_data):
    second_id = accounts.create("ozon", "Временный")
    sync.sync_account(accounts.get(second_id))
    assert db.query_one("SELECT COUNT(*) AS c FROM postings WHERE account_id = ?", (second_id,))["c"]

    accounts.delete(second_id)
    assert accounts.get(second_id) is None
    for table in ("postings", "posting_items", "products", "product_barcodes", "returns"):
        left = db.query_one(f"SELECT COUNT(*) AS c FROM {table} WHERE account_id = ?", (second_id,))["c"]
        assert left == 0, f"в {table} остались данные удалённого кабинета"


def test_avito_cabinet_uses_avito_client():
    account_id = accounts.create("avito", "Avito-магазин")
    account = accounts.get(account_id)
    assert accounts.is_demo(account)
    assert isinstance(avito.get_client(account), avito.DemoAvitoClient)
