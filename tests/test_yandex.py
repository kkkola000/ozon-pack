"""Заказы Яндекс Маркета: загрузка, вкладки, ярлыки, отметка сборки.

Главное требование то же, что у Avito: сборщик видит только заказы в работе —
«Ожидает сборки» и «Ожидает отгрузки», — а всё, что уехало, панель не хранит.
"""
import io
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.core import accounts, db, labels
from app.markets.yandex import client as yandex
from app.main import app
from app.markets.yandex import pack as yandex_pack
from app.markets.yandex import sync as yandex_sync
from app.markets.yandex import store as yandex_store


@pytest.fixture
def market(yandex_account):
    yandex_sync.sync_yandex(yandex_account)
    return yandex_account


@pytest.fixture
def client(market):
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/pack"})
        page = test_client.get("/pack")
        test_client.headers["X-CSRF-Token"] = re.search(
            r'name="csrf-token" content="([^"]*)"', page.text
        ).group(1)
        switched = test_client.post(
            "/api/account/switch", json={"account_id": market["id"], "next": "/yandex"}
        )
        assert switched.status_code == 200, switched.text
        yield test_client


def orders_in(account, substatus=None):
    sql = "SELECT * FROM yandex_orders WHERE account_id = ?"
    params = [account["id"]]
    if substatus:
        sql += " AND substatus = ?"
        params.append(substatus)
    return [dict(r) for r in db.query(sql + " ORDER BY id", params)]


# ------------------------------------------------------------------ синхронизация
def test_only_work_substatuses_are_stored(market):
    rows = orders_in(market)
    assert rows, "заказы Маркета должны загрузиться"
    assert {r["substatus"] for r in rows} <= set(yandex.WORK_SUBSTATUSES)
    assert {r["status"] for r in rows} == {yandex.STATUS_PROCESSING}
    assert orders_in(market, yandex.SUBSTATUS_STARTED)
    assert orders_in(market, yandex.SUBSTATUS_READY_TO_SHIP)


def test_left_orders_are_dropped_even_if_market_returns_them(market):
    """Фильтр в запросе — не защита: чужой этап отсеивается и у себя."""
    client = yandex.get_client(market)
    gone = [o for o in client._orders.values() if o["status"] != "PROCESSING"]
    for order in gone:
        order["status"] = "PROCESSING"     # Маркет вдруг отдал их мимо фильтра
    client.ignore_filter = True
    result = yandex_sync.sync_yandex(market)
    assert result.get("yandex_skipped") == len(gone)
    assert not {str(o["orderId"]) for o in gone} & {r["id"] for r in orders_in(market)}


def test_items_come_with_barcodes_from_the_ozon_catalogue(market, sample_data):
    """Штрихкодов у Маркета нет — их даёт каталог Ozon по артикулу продавца."""
    order = orders_in(market)[0]
    items = yandex_store.yandex_items(market["id"], order["id"])
    assert items
    assert all(item["offer_id"] for item in items)
    assert all(item["barcodes"] for item in items), "артикулы подделки совпадают с каталогом Ozon"
    assert order["positions_count"] == len(items)
    assert order["items_count"] == sum(i["quantity"] for i in items)


def test_without_a_catalogue_items_have_no_barcodes(market):
    order = orders_in(market)[0]
    assert all(not item["barcodes"] for item in yandex_store.yandex_items(market["id"], order["id"]))


def test_order_leaving_work_substatus_disappears(market):
    order = orders_in(market)[0]
    fake = yandex.get_client(market)
    fake._orders[order["id"]]["status"] = "DELIVERY"
    fake._orders[order["id"]]["substatus"] = "DELIVERY_SERVICE_RECEIVED"
    result = yandex_sync.sync_yandex(market)
    assert result.get("yandex_gone") == 1
    assert order["id"] not in {r["id"] for r in orders_in(market)}
    assert not db.query("SELECT 1 FROM yandex_order_items WHERE order_id = ?", (order["id"],))


def test_sync_keeps_local_marks(market, user):
    order = orders_in(market)[0]
    db.execute(
        "UPDATE yandex_orders SET local_state = 'packed', packed_by = ?, packed_at = ?, label_saved_at = ? "
        "WHERE account_id = ? AND id = ?",
        (user["login"], db.now_iso(), db.now_iso(), market["id"], order["id"]),
    )
    yandex_sync.sync_yandex(market)
    row = db.query_one("SELECT * FROM yandex_orders WHERE account_id = ? AND id = ?", (market["id"], order["id"]))
    assert row["local_state"] == "packed"
    assert row["packed_by"] == user["login"]
    assert row["label_saved_at"]


def test_market_dates_are_normalised(market):
    """«ДД-ММ-ГГГГ ЧЧ:ММ:СС» Маркета хранится как ISO UTC — иначе срочность не посчитать."""
    order = yandex_store.yandex_view(orders_in(market)[0])
    assert order["shipment_date"].endswith("+00:00")
    assert order["created_at_api"].endswith("+00:00")
    assert order["urgency"] in {"soon", "ok", "urgent"}
    assert order["hours_left"] is not None
    assert order["deadline_local"].count(".") == 1          # «ДД.ММ»
    assert order["created_local"] != order["created_at_api"]

    raw = {"orderId": 1, "creationDate": "12-11-2025 18:30:15",
           "delivery": {"shipment": {"shipmentDate": "13-11-2025"}}, "items": []}
    with db.write() as conn:
        yandex_store.upsert_yandex_order(conn, market["id"], raw)
    row = db.query_one("SELECT * FROM yandex_orders WHERE account_id = ? AND id = '1'", (market["id"],))
    assert row["created_at_api"] == "2025-11-12T15:30:15+00:00"      # московское 18:30 -> UTC
    assert row["shipment_date"] == "2025-11-13T20:59:59+00:00"       # конец дня по Москве


def test_orders_are_separated_by_cabinet(market):
    second = accounts.get(accounts.create("yandex", "Второй Маркет", "9000002", "token-2"))
    yandex_sync.sync_yandex(second)
    first_ids = {r["id"] for r in orders_in(market)}
    second_ids = {r["id"] for r in orders_in(second)}
    assert first_ids and second_ids
    assert not first_ids & second_ids


# ------------------------------------------------------------------ страницы
def test_tabs_split_work_and_packed(client, market, user):
    started = orders_in(market, yandex.SUBSTATUS_STARTED)[0]
    db.execute(
        "UPDATE yandex_orders SET local_state = 'packed', packed_by = ?, packed_at = ? WHERE account_id = ? AND id = ?",
        (user["login"], db.now_iso(), market["id"], started["id"]),
    )
    pack = client.get("/api/yandex/orders?tab=pack").json()
    packed = client.get("/api/yandex/orders?tab=packed").json()
    ship = client.get("/api/yandex/orders?tab=ship").json()
    assert started["id"] not in {o["id"] for o in pack["orders"]}
    assert [o["id"] for o in packed["orders"]] == [started["id"]]
    assert all(o["substatus"] == yandex.SUBSTATUS_READY_TO_SHIP for o in ship["orders"])
    assert pack["counts"]["packed"] == 1

    page = client.get("/yandex?tab=packed")
    assert page.status_code == 200
    assert "Собрал: admin" in page.text
    assert 'data-reset="' in page.text


def test_pages_open_and_nav_shows_market(client):
    page = client.get("/yandex")
    assert page.status_code == 200
    assert "Заказы Маркета" in page.text
    assert "Заказы FBS" not in page.text
    pack = client.get("/yandex/pack")
    assert pack.status_code == 200
    assert 'id="label-gate"' in pack.text
    assert 'id="scan-panel"' in pack.text
    settings_page = client.get("/settings")
    assert settings_page.status_code == 200
    assert "Ждут сборки" in settings_page.text
    assert client.get("/logs").status_code == 200
    assert client.get("/reports").status_code == 200


def test_ozon_sections_refuse_a_market_cabinet(client):
    assert client.get("/pack").status_code == 409
    assert client.get("/orders").status_code == 409
    assert client.get("/avito").status_code == 409


def test_switch_lands_on_market_pack_page(client, market):
    response = client.post("/api/account/switch", json={"account_id": market["id"], "next": "/orders"})
    assert response.json()["redirect"] == "/yandex/pack"
    response = client.post("/api/account/switch", json={"account_id": market["id"], "next": "/reports"})
    assert response.json()["redirect"] == "/reports"


def test_home_redirects_to_market_pack(client):
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/yandex/pack"


def test_only_a_manager_can_unmark_a_packed_order(client, market, user, other_user):
    order = orders_in(market)[0]
    db.execute(
        "UPDATE yandex_orders SET local_state = 'packed', packed_by = ?, packed_at = ? WHERE account_id = ? AND id = ?",
        (user["login"], db.now_iso(), market["id"], order["id"]),
    )
    response = client.post(f"/api/yandex/orders/{order['id']}/reset")
    assert response.status_code == 200, response.text
    row = db.query_one("SELECT local_state, packed_by FROM yandex_orders WHERE id = ?", (order["id"],))
    assert row["local_state"] == "new" and row["packed_by"] is None
    assert db.query_one("SELECT 1 FROM events WHERE kind = 'yandex_order_reset'")

    with TestClient(app, follow_redirects=False) as packer:
        packer.post("/login", data={"login": "petrov", "password": "secret123", "next": "/pack"})
        page = packer.get("/pack")
        packer.headers["X-CSRF-Token"] = re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1)
        packer.post("/api/account/switch", json={"account_id": market["id"], "next": "/yandex"})
        denied = packer.post(f"/api/yandex/orders/{order['id']}/reset")
        assert denied.status_code == 403


def test_sync_button_reports_count(client):
    response = client.post("/api/yandex/sync")
    assert response.status_code == 200, response.text
    assert response.json()["result"]["yandex"] > 0


# ------------------------------------------------------------------ ярлыки
def test_pending_labels_cover_every_order_in_work(market):
    pending = yandex_pack.pending_labels(market["id"])
    assert set(pending) == {r["id"] for r in orders_in(market)}
    assert labels.state(yandex_pack.pending_labels(market["id"]))["locked"] is True


def test_a_packed_order_does_not_hold_the_lock(market, user):
    for row in orders_in(market):
        db.execute(
            "UPDATE yandex_orders SET local_state = 'packed', packed_by = ?, packed_at = ? WHERE id = ?",
            (user["login"], db.now_iso(), row["id"]),
        )
    assert labels.state(yandex_pack.pending_labels(market["id"])) == {"pending": 0, "locked": False}


def test_archive_opens_the_lock_and_has_a_file_per_order(client, market):
    response = client.post("/api/yandex/labels/archive.zip")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = sorted(archive.namelist())
    assert names == sorted(f"{r['id']}.pdf" for r in orders_in(market))
    assert labels.state(yandex_pack.pending_labels(market["id"])) == {"pending": 0, "locked": False}
    assert db.query_one("SELECT 1 FROM events WHERE kind = 'yandex_labels_archive'")

    again = client.post("/api/yandex/labels/archive.zip")
    assert again.status_code == 400


def test_new_order_locks_the_scanner_again(client, market):
    client.post("/api/yandex/labels/archive.zip")
    fake = yandex.get_client(market)
    fresh = fake._make_order(20, __import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    fresh["status"], fresh["substatus"] = "PROCESSING", yandex.SUBSTATUS_STARTED
    fake._orders[str(fresh["orderId"])] = fresh
    yandex_sync.sync_yandex(market)
    state = client.get("/api/yandex/pack/state").json()["labels"]
    assert state == {"pending": 1, "locked": True}


def test_single_label_and_batch_print(client, market):
    order = orders_in(market)[0]
    one = client.get(f"/api/yandex/label/{order['id']}.pdf")
    assert one.status_code == 200
    assert one.content.startswith(b"%PDF")
    row = db.query_one("SELECT print_count, printed_at FROM yandex_orders WHERE id = ?", (order["id"],))
    assert row["print_count"] == 1 and row["printed_at"]

    ids = [r["id"] for r in orders_in(market)[:2]]
    batch = client.post("/api/yandex/labels.pdf", json={"order_ids": ids})
    assert batch.status_code == 200
    assert batch.content.startswith(b"%PDF")
    assert client.post("/api/yandex/labels.pdf", json={"order_ids": []}).status_code == 400
    assert client.get("/api/yandex/label/000.pdf").status_code == 404


def test_settings_probe_and_test_keys(client, market):
    response = client.post(f"/api/accounts/{market['id']}/test")
    assert response.status_code == 200, response.text
    assert response.json()["result"]["fake"] is True

    created = client.post(
        "/api/accounts",
        json={"marketplace": "yandex", "title": "Ещё Маркет", "client_id": "9000009",
              "api_key": "tok", "skip_test": True},
    )
    assert created.status_code == 200, created.text
    assert accounts.get(created.json()["account_id"])["marketplace"] == "yandex"
