"""Заказы Яндекс Маркета: загрузка, вкладки, ярлыки, отметка сборки.

Главное требование то же, что у Avito: сборщик видит только заказы в работе —
«Ожидает сборки» и «Ожидает отгрузки», — а всё, что уехало, панель не хранит.
"""
import io
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.core import accounts, board, db, labels
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
    waiting = board.rows([market], "packaging")
    packed = board.rows([market], "packed")
    ship = board.rows([market], "deliver")
    ready = {r["id"] for r in orders_in(market, yandex.SUBSTATUS_READY_TO_SHIP)}
    assert started["id"] not in {o["id"] for o in waiting}
    assert [o["id"] for o in packed] == [started["id"]]
    assert ship and {o["id"] for o in ship} <= ready
    assert board.counts([market])[(market["id"], "packed")] == 1

    page = client.get(f"/orders?shop={market['id']}&status=packed")
    assert page.status_code == 200
    assert "Собрал: admin" in page.text
    assert "data-reset" in page.text


def test_pages_open_and_nav_shows_market(client):
    """В шапке — общие «Заказы», а не «Заказы Маркета» или «Заказы FBS»."""
    assert client.get("/yandex").headers["location"] == "/orders?status=packaging"
    page = client.get("/orders")
    assert page.status_code == 200
    assert 'href="/orders"' in page.text
    assert "Заказы Маркета" not in page.text
    assert "Заказы FBS" not in page.text
    assert client.get("/yandex/pack").headers["location"] == "/pack"
    pack = client.get("/pack")
    assert pack.status_code == 200
    assert 'id="label-gate"' in pack.text
    assert 'id="scan-panel"' in pack.text
    settings_page = client.get("/settings")
    assert settings_page.status_code == 200
    assert "Ждут сборки" in settings_page.text
    assert client.get("/logs").status_code == 200
    assert client.get("/reports").status_code == 200


def test_shared_sections_open_in_a_market_cabinet(client):
    """«Сборка» и «Заказы» — общие на все площадки и открываются в любом кабинете.

    Кабинет в шапке их не трогает: что показывать, решают их фильтры.
    """
    assert client.get("/orders").status_code == 200
    assert client.get("/avito").status_code == 303
    assert client.get("/pack").status_code == 200


def test_there_is_no_cabinet_switcher(client, market):
    """Переключателя кабинетов нет: ни списка в шапке, ни адреса переключения."""
    page = client.get("/pack").text
    assert 'id="cabinet-select"' not in page
    assert client.post("/api/account/switch", json={"account_id": market["id"], "next": "/orders"}).status_code in (404, 405)


def test_home_redirects_to_the_shared_pack(client):
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/pack"


def test_only_a_manager_can_unmark_a_packed_order(client, market, user, other_user):
    order = orders_in(market)[0]
    db.execute(
        "UPDATE yandex_orders SET local_state = 'packed', packed_by = ?, packed_at = ? WHERE account_id = ? AND id = ?",
        (user["login"], db.now_iso(), market["id"], order["id"]),
    )
    response = client.post("/api/orders/reset", json={"account_id": market["id"], "id": order["id"]})
    assert response.status_code == 200, response.text
    row = db.query_one("SELECT local_state, packed_by FROM yandex_orders WHERE id = ?", (order["id"],))
    assert row["local_state"] == "new" and row["packed_by"] is None
    assert db.query_one("SELECT 1 FROM events WHERE kind = 'yandex_order_reset'")

    with TestClient(app, follow_redirects=False) as packer:
        packer.post("/login", data={"login": "petrov", "password": "secret123", "next": "/pack"})
        page = packer.get("/pack")
        packer.headers["X-CSRF-Token"] = re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1)
        packer.post("/api/account/switch", json={"account_id": market["id"], "next": "/yandex"})
        denied = packer.post("/api/orders/reset", json={"account_id": market["id"], "id": order["id"]})
        assert denied.status_code == 403


def test_sync_button_updates_the_filtered_cabinet(client, market):
    """«Обновить заказы» в «Заказах» — кабинеты под фильтром, а не тот, что в шапке."""
    response = client.post(f"/api/orders/sync?shop={market['id']}")
    assert response.status_code == 200, response.text
    assert response.json()["updated"] == [market["title"]]
    assert orders_in(market)


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
    response = client.post(f"/api/pack/labels.zip?shop={market['id']}")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = sorted(archive.namelist())
    assert names == sorted(f"{r['id']}.pdf" for r in orders_in(market))
    assert labels.state(yandex_pack.pending_labels(market["id"])) == {"pending": 0, "locked": False}
    assert db.query_one("SELECT 1 FROM events WHERE kind = 'labels_archive'")

    again = client.post(f"/api/pack/labels.zip?shop={market['id']}")
    assert again.status_code == 400


def test_new_order_locks_the_scanner_again(client, market):
    client.post(f"/api/pack/labels.zip?shop={market['id']}")
    fake = yandex.get_client(market)
    fresh = fake._make_order(20, __import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    fresh["status"], fresh["substatus"] = "PROCESSING", yandex.SUBSTATUS_STARTED
    fake._orders[str(fresh["orderId"])] = fresh
    yandex_sync.sync_yandex(market)
    state = client.get(f"/api/pack/state?shop={market['id']}").json()["labels"]
    assert state["pending"] == 1 and state["locked"] is True


def test_single_label_and_batch_print(client, market):
    order = orders_in(market)[0]
    def labels(ids):
        return client.post("/api/orders/labels.pdf", json={"account_id": market["id"], "ids": ids})

    one = labels([order["id"]])
    assert one.status_code == 200, one.text
    assert one.content.startswith(b"%PDF")
    row = db.query_one("SELECT print_count, printed_at FROM yandex_orders WHERE id = ?", (order["id"],))
    assert row["print_count"] == 1 and row["printed_at"]

    ids = [r["id"] for r in orders_in(market)[:2]]
    batch = labels(ids)
    assert batch.status_code == 200
    assert batch.content.startswith(b"%PDF")
    assert labels([]).status_code == 400
    assert labels(["000"]).status_code == 404


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


def test_returns_section_is_closed_for_the_market(client, market):
    """Возвраты Маркет в панель не отдаёт — раздел ему не показывается.

    Отказ должен быть понятным: это не поломка, а «у этой площадки такого нет».
    """
    # В фильтре «Возвратов» кабинета Маркета нет, а его адрес — это «Все кабинеты».
    response = client.get(f"/returns?shop={market['id']}")
    assert response.status_code == 200
    assert f'href="/returns?shop={market["id"]}"' not in response.text
