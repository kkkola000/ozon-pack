"""Заказы Avito: загрузка, подтверждение, отправка, этикетки.

Проверяем главное требование: сборщик видит только «Подтвердите заказ» и
«Отправьте заказ», а всё остальное панель не показывает и не хранит.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app import accounts, avito, db, store, sync
from app.main import app


@pytest.fixture
def avito_account():
    account = accounts.get(accounts.create("avito", "Avito демо"))
    sync.sync_avito(account)
    return account


@pytest.fixture
def client(avito_account):
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/pack"})
        page = test_client.get("/pack")
        test_client.headers["X-CSRF-Token"] = re.search(
            r'name="csrf-token" content="([^"]*)"', page.text
        ).group(1)
        # Переключаемся на кабинет Avito тем же способом, что и человек в шапке.
        switched = test_client.post(
            "/api/account/switch", json={"account_id": avito_account["id"], "next": "/avito"}
        )
        assert switched.status_code == 200, switched.text
        yield test_client


def orders_in(account, status):
    return db.query(
        "SELECT * FROM avito_orders WHERE account_id = ? AND status = ?", (account["id"], status)
    )


# ------------------------------------------------------------------ синхронизация
def test_only_work_statuses_are_stored(avito_account):
    """В панель попадают только заказы, с которыми сборщику надо что-то сделать."""
    statuses = {row["status"] for row in db.query(
        "SELECT DISTINCT status FROM avito_orders WHERE account_id = ?", (avito_account["id"],)
    )}
    assert statuses
    assert statuses <= set(avito.WORK_STATUSES), f"лишние статусы в панели: {statuses}"


def test_order_items_are_saved(avito_account):
    order = orders_in(avito_account, avito.STATUS_ON_CONFIRMATION)[0]
    items = store.avito_items(avito_account["id"], order["id"])
    assert items, "у заказа должен быть состав"
    assert all(item["title"] for item in items)
    assert order["positions_count"] == len(items)


def test_order_leaving_work_status_disappears(avito_account):
    """Заказ уехал в «в пути» — из панели он уходит, сборщику там делать нечего."""
    order = orders_in(avito_account, avito.STATUS_READY_TO_SHIP)[0]
    client = avito.get_client(avito_account)
    client._orders[order["id"]]["status"] = avito.STATUS_IN_TRANSIT

    sync.sync_avito(avito_account)
    left = db.query_one(
        "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND id = ?",
        (avito_account["id"], order["id"]),
    )["c"]
    assert left == 0
    items_left = db.query_one(
        "SELECT COUNT(*) AS c FROM avito_order_items WHERE account_id = ? AND order_id = ?",
        (avito_account["id"], order["id"]),
    )["c"]
    assert items_left == 0, "состав удалённого заказа не должен оставаться"


# ------------------------------------------------------------------ действия
def test_confirm_moves_order_to_ship_tab(client, avito_account):
    order = orders_in(avito_account, avito.STATUS_ON_CONFIRMATION)[0]
    response = client.post("/api/avito/confirm", json={"order_ids": [order["id"]]})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"

    row = db.query_one(
        "SELECT * FROM avito_orders WHERE account_id = ? AND id = ?", (avito_account["id"], order["id"])
    )
    assert row["status"] == avito.STATUS_READY_TO_SHIP
    assert row["confirmed_by"] == "admin"
    # Действия перечитаны у площадки: кнопки «Подтвердить» на заказе больше нет.
    assert "confirm" not in (row["actions"] or "")
    assert db.query_one("SELECT COUNT(*) AS c FROM events WHERE kind = 'avito_confirm'")["c"] == 1


def test_ship_removes_order_from_panel(client, avito_account):
    """После отправки заказ уходит из рабочих статусов — и из панели тоже."""
    order = next(
        row for row in orders_in(avito_account, avito.STATUS_READY_TO_SHIP)
        if "perform" in (row["actions"] or "")
    )
    response = client.post("/api/avito/ship", json={"order_ids": [order["id"]]})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"

    assert avito.get_client(avito_account)._orders[order["id"]]["status"] == avito.STATUS_IN_TRANSIT
    left = db.query_one(
        "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND id = ?",
        (avito_account["id"], order["id"]),
    )["c"]
    assert left == 0
    assert db.query_one("SELECT COUNT(*) AS c FROM events WHERE kind = 'avito_ship'")["c"] == 1


def test_double_confirm_is_reported_not_silent(client, avito_account):
    """Повторное подтверждение — ошибка Avito, и её видно оператору."""
    order = orders_in(avito_account, avito.STATUS_ON_CONFIRMATION)[0]
    client.post("/api/avito/confirm", json={"order_ids": [order["id"]]})

    # Возвращаем локальный статус, как будто список ещё не обновился.
    db.execute(
        "UPDATE avito_orders SET status = ? WHERE account_id = ? AND id = ?",
        (avito.STATUS_ON_CONFIRMATION, avito_account["id"], order["id"]),
    )
    response = client.post("/api/avito/confirm", json={"order_ids": [order["id"]]})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert "уже подтверждён" in body["results"][0]["message"]
    assert db.query_one("SELECT COUNT(*) AS c FROM events WHERE kind = 'avito_error'")["c"] == 1


def test_order_from_another_cabinet_is_not_touched(client, avito_account):
    """Чужой заказ не подтвердить: кабинеты изолированы."""
    other = accounts.get(accounts.create("avito", "Второй Avito"))
    sync.sync_avito(other)
    foreign = db.query_one(
        "SELECT id FROM avito_orders WHERE account_id = ? LIMIT 1", (other["id"],)
    )["id"]

    response = client.post("/api/avito/confirm", json={"order_ids": [foreign]})
    assert response.status_code == 404


# ------------------------------------------------------------------ этикетки
def test_label_returns_pdf_and_counts_print(client, avito_account):
    order = orders_in(avito_account, avito.STATUS_READY_TO_SHIP)[0]
    response = client.get(f"/api/avito/label/{order['id']}.pdf")
    assert response.status_code == 200
    assert response.content[:4] == b"%PDF"

    row = db.query_one(
        "SELECT printed_at, print_count FROM avito_orders WHERE account_id = ? AND id = ?",
        (avito_account["id"], order["id"]),
    )
    assert row["printed_at"] and row["print_count"] == 1


def test_labels_batch_is_limited(client, avito_account):
    response = client.post("/api/avito/labels.pdf", json={"order_ids": [str(i) for i in range(51)]})
    assert response.status_code == 400


# ------------------------------------------------------------------ страница
def test_page_shows_only_two_tabs(client):
    page = client.get("/avito")
    assert page.status_code == 200
    assert "Подтвердите заказ" in page.text
    assert "Отправьте заказ" in page.text
    for hidden in ("Отменить заказ", "Честный знак", "Трек-номер", "интервал курьера"):
        assert hidden not in page.text, f"в интерфейсе не должно быть «{hidden}»"


def test_avito_sections_are_closed_for_ozon_cabinet(client):
    """В кабинете Ozon раздел Avito недоступен — и наоборот."""
    ozon_account = accounts.all_accounts()[0]
    assert ozon_account["marketplace"] == "ozon"
    switched = client.post("/api/account/switch", json={"account_id": ozon_account["id"], "next": "/avito"})
    assert switched.status_code == 200
    assert switched.json()["redirect"] == "/pack"
    assert client.get("/avito").status_code == 409


# ------------------------------------------------------------------ клиент
def test_client_paginates_orders(avito_account):
    client = avito.get_client(avito_account)
    everything = client.orders_all(statuses=list(avito.WORK_STATUSES))
    first_page, has_more = client.orders(statuses=list(avito.WORK_STATUSES), page=1, limit=2)
    assert len(first_page) == 2
    assert has_more is (len(everything) > 2)
    assert len(everything) >= len(first_page)


def test_limit_is_capped_at_twenty(avito_account):
    """У Avito ограничение limit ≤ 20 — клиент не должен его нарушать."""
    client = avito.AvitoClient(client_id="x", client_secret="y")
    captured = {}

    def fake(method, path, **kwargs):
        captured["params"] = kwargs.get("params")
        return {"orders": [], "hasMore": False}

    client.request_json = fake  # type: ignore[assignment]
    client.orders(limit=1000)
    assert dict(captured["params"])["limit"] == 20
    client.close()


def test_sync_all_covers_both_marketplaces(avito_account):
    """Фоновый поток обходит все кабинеты: и Ozon, и Avito."""
    result = sync.sync_all()
    assert result.get("saved"), "отправления Ozon не загрузились"
    assert result.get("avito"), "заказы Avito не загрузились"
    assert result.get("accounts") == 2
    assert "errors" not in result


def test_one_broken_cabinet_does_not_stop_others(avito_account, monkeypatch):
    """Кабинет без связи не должен ронять синхронизацию остальных."""
    from app import ozon

    def boom(*args, **kwargs):
        raise ozon.OzonError("нет связи")

    monkeypatch.setattr(ozon.DemoOzonClient, "posting_list", boom)
    result = sync.sync_all()
    assert result.get("avito"), "заказы Avito должны загрузиться несмотря на сбой Ozon"
    assert result.get("errors")
