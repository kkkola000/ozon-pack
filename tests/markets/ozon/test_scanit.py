"""Новая этикетка FBS: штрихкод scanit.

С осени 2026 Ozon печатает новые этикетки FBS со своим штрихкодом — полем
scanit отправления (/v4/posting/fbs/list, /v3/posting/fbs/get). Сканер читает
с этикетки именно его. Панель, знавшая только верхний и нижний штрихкоды
старой этикетки, такой скан не узнала бы: сборка не открылась бы стикером и
не закрылась бы им.
"""
import json

import pytest

from app.core import db
from app.markets.ozon import pack as packing
from app.markets.ozon import store as ozon_store
from tests.conftest import barcode_of, pick_posting

POSTING = {
    "posting_number": "0208245774-0031-1", "order_number": "0208245774-0031",
    "status": "awaiting_deliver", "shipment_date": "2026-10-10T12:00:00.000Z",
    "barcodes": {"lower_barcode": "601302481800031", "upper_barcode": "%0302481800031"},
    "scanit": "SCANIT-0208245774-0031",
    "products": [{"name": "Смартфон", "offer_id": "b4408110", "price": {"amount": "12990.00", "currency": "RUB"},
                  "quantity": 1, "sku": 1904686181}],
}


def save(account, raw):
    with db.write() as conn:
        ozon_store.upsert_posting(conn, account["id"], json.loads(json.dumps(raw)))
    return db.query_one("SELECT * FROM postings WHERE account_id = ? AND posting_number = ?",
                        (account["id"], raw["posting_number"]))


def scanit_of(account, number) -> str:
    row = db.query_one("SELECT scanit FROM postings WHERE account_id = ? AND posting_number = ?",
                       (account["id"], number))
    assert row["scanit"], f"у отправления {number} не сохранился scanit"
    return row["scanit"]


# ------------------------------------------------------------------ хранение
def test_scanit_is_stored_with_the_posting(account):
    row = save(account, POSTING)
    assert row["scanit"] == "SCANIT-0208245774-0031"
    assert row["barcode_lower"] == "601302481800031", "штрихкоды старой этикетки на месте"


def test_scanit_comes_from_the_posting_list(account, sample_data):
    fake = sample_data
    number, posting = next((n, p) for n, p in fake._postings.items() if p["status"] == "awaiting_deliver")
    assert scanit_of(account, number) == posting["scanit"]


def test_an_answer_without_scanit_does_not_wipe_it(account):
    """get-by-barcode и старые ответы поля не несут — известный штрихкод остаётся."""
    save(account, POSTING)
    without = {key: value for key, value in POSTING.items() if key != "scanit"}
    assert save(account, without)["scanit"] == "SCANIT-0208245774-0031"
    assert save(account, {**POSTING, "scanit": "SCANIT-NEW"})["scanit"] == "SCANIT-NEW"


def test_scanit_as_a_number_is_kept_and_anything_else_is_ignored(account):
    assert save(account, {**POSTING, "scanit": 4600123})["scanit"] == "4600123"
    other = {**POSTING, "posting_number": "0208245774-0032-1", "scanit": {"value": "x"}}
    assert save(account, other)["scanit"] is None


# ------------------------------------------------------------------ сборка
@pytest.fixture
def offline(sample_data, monkeypatch):
    """Ozon по штрихкоду не спрашиваем: известную этикетку панель узнаёт сама.

    Запасной путь — get-by-barcode — есть, но это поход в сеть на каждый скан,
    а общая «Сборка» спрашивает кабинеты, чей код, вовсе без сети.
    """
    def refuse(barcode):
        raise AssertionError(f"этикетку {barcode} панель не узнала сама и пошла в Ozon")

    monkeypatch.setattr(sample_data, "posting_by_barcode", refuse)
    return sample_data


def test_new_label_opens_and_closes_the_posting(account, offline, user):
    posting = pick_posting(positions=1)
    number = posting["posting_number"]
    label = scanit_of(account, number)

    opened = packing.scan(account, user, label)
    assert opened["state"]["active"]["posting_number"] == number, opened["message"]

    for item in posting["items"]:
        for _ in range(item["quantity"]):
            assert packing.scan(account, user, barcode_of(item["sku"]))["status"] == "ok"

    done = packing.scan(account, user, label)
    assert done["action"] == "completed", done["message"]
    row = db.query_one("SELECT local_state FROM postings WHERE account_id = ? AND posting_number = ?",
                       (account["id"], number))
    assert row["local_state"] == "packed"


def test_new_label_of_another_posting_is_a_stop(account, offline, user):
    first = pick_posting(positions=1)
    packing.select_posting(account, user, first["posting_number"])
    other = db.query_one(
        "SELECT posting_number, scanit FROM postings WHERE account_id = ? AND status = ? "
        "AND posting_number != ? AND scanit IS NOT NULL LIMIT 1",
        (account["id"], ozon_store.STATUS_AWAITING_DELIVER, first["posting_number"]),
    )
    result = packing.scan(account, user, other["scanit"])
    assert result["action"] == "wrong_label", result["message"]
    assert packing.load_state(account, user)["active"]["posting_number"] == first["posting_number"]


def test_the_cabinet_recognises_its_new_label(account, sample_data, user):
    """Общая «Сборка» спрашивает кабинеты, чей это код, — ответ тот же, что у скана."""
    posting = pick_posting(positions=1)
    number = posting["posting_number"]
    assert packing.owner(account, user, scanit_of(account, number)) == ("label", number)


def test_unknown_new_label_is_asked_from_ozon(account, sample_data, user):
    """Отправления ещё нет в панели — get-by-barcode принимает и scanit."""
    posting = pick_posting(positions=1)
    number = posting["posting_number"]
    label = scanit_of(account, number)
    db.execute("DELETE FROM posting_items WHERE account_id = ? AND posting_number = ?", (account["id"], number))
    db.execute("DELETE FROM postings WHERE account_id = ? AND posting_number = ?", (account["id"], number))

    result = packing.scan(account, user, label)
    assert result["state"]["active"]["posting_number"] == number, result["message"]
    assert scanit_of(account, number) == label
