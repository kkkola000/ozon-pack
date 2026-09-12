"""Кабинеты: ключи площадок, демо-режим и разделение данных."""
import pytest

from app import accounts, avito, db, ozon, sync
from app.config import settings


@pytest.fixture
def ozon_account():
    return accounts.default_account()


# В тестах фабрики клиентов подменены подделками — здесь нужна настоящая.
_real_ozon_client = ozon.get_client
_real_avito_client = avito.get_client


def test_default_account_created_on_init(ozon_account):
    assert ozon_account is not None
    assert ozon_account["marketplace"] == "ozon"
    assert ozon_account["active"] == 1


def test_cabinet_without_keys_has_no_client(monkeypatch, ozon_account):
    """Без ключей панель не выдумывает данные, а честно отказывает."""
    monkeypatch.setattr(settings, "ozon_client_id", "")
    monkeypatch.setattr(settings, "ozon_api_key", "")
    accounts.update(ozon_account["id"], client_id="", api_key="")
    account = accounts.get(ozon_account["id"])

    assert not accounts.is_configured(account)
    assert accounts.credentials(account)[2] == "none"
    with pytest.raises(ozon.OzonError) as failure:
        _real_ozon_client(account)
    assert "ключи" in str(failure.value)


def test_panel_keys_win_over_env(monkeypatch, ozon_account):
    monkeypatch.setattr(settings, "ozon_client_id", "env-id")
    monkeypatch.setattr(settings, "ozon_api_key", "env-key")
    accounts.update(ozon_account["id"], client_id="", api_key="")
    assert accounts.credentials(accounts.get(ozon_account["id"])) == ("env-id", "env-key", "env")

    accounts.update(ozon_account["id"], client_id="panel-id", api_key="panel-key")
    updated = accounts.get(ozon_account["id"])
    assert accounts.credentials(updated) == ("panel-id", "panel-key", "panel")
    assert accounts.is_configured(updated)
    assert _real_ozon_client(updated).client_id == "panel-id"


def test_clear_keys_returns_to_env_then_nothing(monkeypatch, ozon_account):
    monkeypatch.setattr(settings, "ozon_client_id", "env-id")
    monkeypatch.setattr(settings, "ozon_api_key", "env-key")
    accounts.update(ozon_account["id"], client_id="panel-id", api_key="panel-key")

    accounts.update(ozon_account["id"], client_id="", api_key="")
    assert accounts.credentials(accounts.get(ozon_account["id"]))[2] == "env"

    monkeypatch.setattr(settings, "ozon_client_id", "")
    monkeypatch.setattr(settings, "ozon_api_key", "")
    assert not accounts.is_configured(accounts.get(ozon_account["id"]))


def test_key_is_masked():
    assert accounts.mask("abcdefghij").endswith("ghij")
    assert "abcdef" not in accounts.mask("abcdefghij")
    assert accounts.mask("") == ""


def test_api_key_is_encrypted_in_database(ozon_account):
    """Ключ площадки не должен читаться в базе: копия базы = утечка ключей."""
    accounts.update(ozon_account["id"], client_id="cid-1", api_key="секретный-ключ")
    stored = db.query_one("SELECT client_id, api_key FROM accounts WHERE id = ?", (ozon_account["id"],))
    assert "секретный-ключ" not in stored["api_key"]
    assert stored["api_key"].startswith("enc:v1:")
    # Client-Id не секрет — он показывается в настройках и шифровать его незачем.
    assert stored["client_id"] == "cid-1"
    # Панель при этом видит ключ как обычно.
    assert accounts.credentials(accounts.get(ozon_account["id"]))[1] == "секретный-ключ"


def test_plaintext_keys_are_encrypted_on_startup(ozon_account):
    """Ключи из базы прежних версий шифруются при первом запуске."""
    with db.write() as conn:
        conn.execute(
            "UPDATE accounts SET api_key = 'старый-открытый-ключ' WHERE id = ?", (ozon_account["id"],)
        )
    db.init_db()
    stored = db.query_one("SELECT api_key FROM accounts WHERE id = ?", (ozon_account["id"],))["api_key"]
    assert stored.startswith("enc:v1:")
    assert accounts.credentials(accounts.get(ozon_account["id"]))[1] == "старый-открытый-ключ"


def test_lost_secret_does_not_break_panel(ozon_account, monkeypatch):
    """Сменился SECRET_KEY — панель говорит «ключей нет», а не падает."""
    accounts.update(ozon_account["id"], client_id="cid-1", api_key="ключ")
    monkeypatch.setattr(settings, "secret_key", "другой-секрет-совсем")
    assert accounts.credentials(accounts.get(ozon_account["id"]))[1] == ""
    assert not accounts.is_configured(accounts.get(ozon_account["id"]))


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
def test_two_cabinets_keep_data_apart(sample_data):
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


def test_deleting_cabinet_removes_its_data(sample_data):
    second_id = accounts.create("ozon", "Временный")
    sync.sync_account(accounts.get(second_id))
    assert db.query_one("SELECT COUNT(*) AS c FROM postings WHERE account_id = ?", (second_id,))["c"]

    accounts.delete(second_id)
    assert accounts.get(second_id) is None
    for table in ("postings", "posting_items", "products", "product_barcodes", "returns"):
        left = db.query_one(f"SELECT COUNT(*) AS c FROM {table} WHERE account_id = ?", (second_id,))["c"]
        assert left == 0, f"в {table} остались данные удалённого кабинета"


def test_avito_cabinet_without_keys_has_no_client():
    account = accounts.get(accounts.create("avito", "Avito-магазин"))
    assert not accounts.is_configured(account)
    with pytest.raises(avito.AvitoError) as failure:
        _real_avito_client(account)
    assert "ключи" in str(failure.value)


def test_avito_cabinet_with_keys_gets_client():
    account = accounts.get(accounts.create("avito", "Avito-магазин", "id", "secret"))
    assert accounts.is_configured(account)
    client = _real_avito_client(account)
    assert client.client_id == "id"
    client.close()
