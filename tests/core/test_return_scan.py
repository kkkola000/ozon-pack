"""Приёмка возвратов сканером: стикер или штрихкод возврата, затем товар.

Сборщик вернулся из пункта выдачи с пакетом. Первый скан — наклейка на
пакете: панель находит возврат в неподтверждённом акте и открывает отметку.
Второй — штрихкод на самом товаре: совпал, и отметка «Принят». Не тот товар
— отметка не меняется, решение «Не принят» остаётся за человеком.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app.core import accounts, catalog, db, product_links, return_acts, store
from app.main import app
from app.markets.avito import client as avito
from app.markets.avito import returns as avito_returns
from app.markets.avito import sync as avito_sync
from app.markets.ozon import client as ozon
from app.markets.ozon import returns as ozon_returns
from tests.conftest import barcode_of


@pytest.fixture
def client(sample_data):
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/returns"})
        page = test_client.get("/returns?tab=acts")
        test_client.headers["X-CSRF-Token"] = re.search(
            r'name="csrf-token" content="([^"]*)"', page.text
        ).group(1)
        yield test_client


def ozon_act() -> list[dict]:
    """Съездили за всем, что лежало в пункте, и свели это в акт: строки акта."""
    account = accounts.default_account()
    ozon.get_client(account).receive()
    ozon_returns.sync_returns(account)
    result = ozon_returns.from_received(account["id"], store.local_day(), user={"login": "admin"})
    assert result["status"] == "ok", result["message"]
    return return_acts.detail(result["act_id"])["ozon"]


def scan(client, code):
    return client.post("/api/returns/scan", json={"code": code})


def goods(client, rows, code):
    """Скан товара по открытым в окне возвратам: rows — ответ первого скана."""
    entries = [{"marketplace": row["marketplace"], "id": row["id"], "account_id": row["account_id"],
                "got": row.get("got", {})} for row in rows]
    return client.post("/api/returns/scan/goods", json={"code": code, "rows": entries})


def stored(row) -> dict:
    return dict(db.query_one(
        "SELECT mark, note, mark_by FROM returns WHERE account_id = ? AND id = ?",
        (row["account_id"], row["id"]),
    ))


# ------------------------------------------------------------ шаг 1: возврат
def test_return_barcode_opens_the_return(client):
    row = ozon_act()[0]
    response = scan(client, row["barcode"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert [found["id"] for found in body["rows"]] == [str(row["id"])]
    card = body["rows"][0]
    assert card["marketplace"] == "ozon" and card["account_id"] == row["account_id"]
    assert card["act_title"].startswith("Возвраты за")
    # Что искать на коробке — штрихкод товара из каталога.
    assert card["goods"][0]["expect"] == barcode_of(row["sku"])
    assert card["goods"][0]["checked"] is True
    assert ["Штрихкод", row["barcode"]] in card["facts"]


def test_return_barcode_is_found_in_any_letter_case(client):
    """Сканер с включённым Caps Lock отдаёт «ret…» вместо «RET…» — это тот же пакет."""
    row = ozon_act()[0]
    assert scan(client, row["barcode"].lower()).status_code == 200


def test_return_number_opens_the_return_too(client):
    row = ozon_act()[0]
    assert scan(client, str(row["id"])).json()["rows"][0]["id"] == str(row["id"])


def test_fbs_sticker_finds_the_returns_of_its_posting(client):
    """Стикер FBS, с которым товар уезжал, — по нему находится отправление, а по нему возврат."""
    row = ozon_act()[0]
    db.execute(
        "INSERT OR REPLACE INTO postings(account_id, posting_number, barcode_upper, barcode_lower, status) "
        "VALUES(?,?,?,?,?)",
        (row["account_id"], row["posting_number"], "%0399999", "OZN999999999", "delivered"),
    )
    for code in ("OZN999999999", "%0399999", row["posting_number"]):
        response = scan(client, code)
        assert response.status_code == 200, (code, response.text)
        assert str(row["id"]) in [found["id"] for found in response.json()["rows"]]


def test_posting_with_several_returns_opens_them_all(client):
    first, second = ozon_act()[:2]
    db.execute("UPDATE returns SET posting_number = ? WHERE account_id = ? AND id = ?",
               (first["posting_number"], second["account_id"], second["id"]))
    body = scan(client, first["posting_number"]).json()
    assert {found["id"] for found in body["rows"]} == {str(first["id"]), str(second["id"])}
    assert body["title"] == f"Возвраты по коду {first['posting_number']}"

    # Второй товар отправления засчитывается своему возврату, а не первому.
    answer = goods(client, body["rows"], barcode_of(second["sku"])).json()
    assert answer["status"] == "ok" and answer["action"] == "accepted", answer
    assert answer["row"]["id"] == str(second["id"])
    assert stored(second)["mark"] == "ok"
    assert stored(first)["mark"] is None


def test_unknown_code_is_refused(client):
    ozon_act()
    response = scan(client, "НЕТ-ТАКОГО-КОДА")
    assert response.status_code == 404
    assert "не найден" in response.json()["detail"]


def test_return_still_at_the_pickup_point_is_explained(client):
    """В акт ещё не попал — отмечать негде: так и сказать, а не «не найден»."""
    row = db.query_one("SELECT * FROM returns WHERE account_id = ? AND is_ready = 1 LIMIT 1",
                       (accounts.default_account()["id"],))
    response = scan(client, row["barcode"])
    assert response.status_code == 409
    assert "ещё не получен" in response.json()["detail"]


def test_received_return_without_an_act_is_explained(client):
    account = accounts.default_account()
    ozon.get_client(account).receive()
    ozon_returns.sync_returns(account)
    row = db.query_one("SELECT * FROM returns WHERE account_id = ? AND received_at IS NOT NULL "
                       "AND act_id IS NULL LIMIT 1", (account["id"],))
    response = scan(client, row["barcode"])
    assert response.status_code == 409
    assert "ещё не в акте" in response.json()["detail"]


def test_confirmed_act_is_not_reopened_by_a_scan(client):
    rows = ozon_act()
    act_id = rows[0]["act_id"]
    db.execute("UPDATE returns SET mark = 'ok' WHERE act_id = ?", (act_id,))
    assert return_acts.confirm(act_id, {"login": "admin"})["status"] == "ok"
    response = scan(client, rows[0]["barcode"])
    assert response.status_code == 409
    assert "подтверждённом акте" in response.json()["detail"]


# ------------------------------------------------------------ шаг 2: товар
def test_matching_product_marks_the_return_accepted(client):
    row = ozon_act()[0]
    db.execute("UPDATE returns SET note = 'пакет мятый' WHERE account_id = ? AND id = ?",
               (row["account_id"], row["id"]))
    rows = scan(client, row["barcode"]).json()["rows"]
    answer = goods(client, rows, barcode_of(row["sku"]))
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["status"] == "ok" and body["action"] == "accepted" and body["done"] is True
    assert body["mark"]["mark_label"] == "Принят"
    # Шапка акта перерисовывается на месте — счётчики приходят сразу.
    assert body["act"]["marked_ok"] == 1

    saved = stored(row)
    assert saved["mark"] == "ok" and saved["mark_by"] == "admin"
    assert saved["note"] == "пакет мятый", "скан товара стёр комментарий"
    event = db.query_one("SELECT message FROM events WHERE kind = 'return_mark' ORDER BY id DESC LIMIT 1")
    assert "скан товара" in event["message"]


def test_sku_and_article_count_as_the_product(client):
    """Штрихкод на товаре стёрся — сборщик сканирует артикул с этикетки."""
    row = ozon_act()[0]
    rows = scan(client, row["barcode"]).json()["rows"]
    assert goods(client, rows, row["offer_id"]).json()["action"] == "accepted"


def test_wrong_product_keeps_the_mark(client):
    first, second = ozon_act()[:2]
    assert first["sku"] != second["sku"]
    rows = scan(client, first["barcode"]).json()["rows"]
    body = goods(client, rows, barcode_of(second["sku"])).json()
    assert body["status"] == "error" and body["action"] == "wrong_product"
    assert body["scanned"] == second["product_name"]
    assert "Отметка не изменилась" in body["message"]
    assert stored(first)["mark"] is None
    assert db.query_one("SELECT COUNT(*) AS c FROM events WHERE kind = 'return_scan_wrong'")["c"] == 1


def test_foreign_code_is_not_the_product(client):
    row = ozon_act()[0]
    rows = scan(client, row["barcode"]).json()["rows"]
    body = goods(client, rows, "0000000000000").json()
    assert body["action"] == "wrong_product"
    assert stored(row)["mark"] is None


def test_two_pieces_need_two_scans(client):
    row = ozon_act()[0]
    db.execute("UPDATE returns SET quantity = 2 WHERE account_id = ? AND id = ?", (row["account_id"], row["id"]))
    rows = scan(client, row["barcode"]).json()["rows"]
    code = barcode_of(row["sku"])

    first = goods(client, rows, code).json()
    assert first["action"] == "counted" and first["done"] is False
    assert "осталось 1 шт." in first["message"]
    assert stored(row)["mark"] is None, "половину возврата приняли целиком"

    rows[0]["got"] = first["got"]
    second = goods(client, rows, code).json()
    assert second["action"] == "accepted"
    assert stored(row)["mark"] == "ok"

    rows[0]["got"] = second["got"]
    assert goods(client, rows, code).json()["action"] == "extra"


def test_linked_card_barcode_counts_as_the_product(client):
    """Коробка одна на полке: штрихкод сопоставленной карточки тоже подходит."""
    row = ozon_act()[0]
    other = accounts.get(accounts.create("ozon", "Второй Ozon", "test-client", "test-key"))
    with db.write() as conn:
        catalog.save(conn, other["id"], [{"sku": "777", "offer_id": "X-777", "name": "Тот же товар",
                                          "image": None, "barcodes": ["4650000000011"]}])
    product_links.link((row["account_id"], row["sku"]), [(other["id"], "777")], user={"login": "admin"})

    rows = scan(client, row["barcode"]).json()["rows"]
    assert goods(client, rows, "4650000000011").json()["action"] == "accepted"


def test_product_without_barcodes_accepts_an_unknown_code_only(client):
    """Сверять не с чем — засчитываем незнакомый код, но не штрихкод другого товара."""
    first, second = ozon_act()[:2]
    db.execute("DELETE FROM product_barcodes WHERE account_id = ? AND sku = ?", (first["account_id"], first["sku"]))
    db.execute("UPDATE products SET barcodes = '[]' WHERE account_id = ? AND sku = ?",
               (first["account_id"], first["sku"]))
    rows = scan(client, first["barcode"]).json()["rows"]
    assert rows[0]["goods"][0]["checked"] is False

    assert goods(client, rows, barcode_of(second["sku"])).json()["action"] == "wrong_product"
    body = goods(client, rows, "2000000000015").json()
    assert body["action"] == "accepted" and body["checked"] is False
    assert "сверить было не с чем" in body["message"]


def test_goods_scan_refuses_a_confirmed_act(client):
    rows_in_act = ozon_act()
    rows = scan(client, rows_in_act[0]["barcode"]).json()["rows"]
    db.execute("UPDATE return_acts SET confirmed_at = ? WHERE id = ?", (db.now_iso(), rows_in_act[0]["act_id"]))
    response = goods(client, rows, barcode_of(rows_in_act[0]["sku"]))
    assert response.status_code == 409


def test_scan_requires_csrf(client):
    row = ozon_act()[0]
    del client.headers["X-CSRF-Token"]
    assert scan(client, row["barcode"]).status_code == 403
    assert client.post("/api/returns/scan/goods", json={"code": "1", "rows": [{}]}).status_code == 403


# ------------------------------------------------------------ Avito
def test_avito_return_is_accepted_through_a_linked_card(client):
    """У Avito своих штрихкодов нет: товар узнаётся по сопоставленной карточке."""
    account = accounts.get(accounts.create("avito", "Кабинет Avito", "test-client", "test-secret"))
    avito_sync.sync_avito(account)
    order = dict(db.query_one("SELECT * FROM avito_orders WHERE account_id = ? AND status = ? LIMIT 1",
                              (account["id"], avito.STATUS_ON_RETURN)))
    avito.get_client(account)._orders[order["id"]]["status"] = avito.STATUS_CLOSED
    avito_sync.sync_avito(account)
    day = db.query_one("SELECT received_day FROM avito_orders WHERE account_id = ? AND id = ?",
                       (account["id"], order["id"]))["received_day"]
    assert return_acts.from_received(avito_returns.SOURCE, account["id"], day, user={"login": "admin"})["act_id"]

    items = db.query("SELECT * FROM avito_order_items WHERE account_id = ? AND order_id = ?",
                     (account["id"], order["id"]))
    ozon_sku = db.query_one("SELECT sku FROM products WHERE account_id = ? LIMIT 1",
                            (accounts.default_account()["id"],))["sku"]
    code = barcode_of(ozon_sku)
    with db.write() as conn:
        catalog.save(conn, account["id"], [{"sku": item["avito_id"], "offer_id": item["seller_id"],
                                            "name": item["title"], "image": None, "barcodes": []}
                                           for item in items])
    for item in items:
        product_links.link((accounts.default_account()["id"], ozon_sku), [(account["id"], item["avito_id"])],
                           user={"login": "admin"})

    body = scan(client, order["return_tracking"]).json()
    assert body["rows"][0]["marketplace"] == "avito"
    rows = body["rows"]
    rows[0]["got"] = {}
    need = sum(item["quantity"] for item in items)
    for _ in range(need):
        answer = goods(client, rows, code).json()
        assert answer["status"] == "ok", answer
        rows[0]["got"] = answer["got"]
    assert answer["action"] == "accepted"
    assert db.query_one("SELECT mark FROM avito_orders WHERE account_id = ? AND id = ?",
                        (account["id"], order["id"]))["mark"] == "ok"


# ------------------------------------------------------------ страница
def test_scan_field_lives_on_the_acts_tab(client):
    page = client.get("/returns?tab=acts").text
    assert 'id="rscan-input"' in page
    assert "Отсканируйте стикер или штрихкод возврата" in page
    assert "/static/return_scan.js" in page
    assert 'id="rscan-input"' not in client.get("/returns").text
