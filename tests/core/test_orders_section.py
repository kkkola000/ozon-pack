"""Раздел «Заказы»: один на все кабинеты, фильтр кабинетов и статусы склада.

Главное, что здесь проверяется: раздел не зависит от кабинета в шапке. Что
показать, решают его фильтры, а действие уходит в кабинет своего заказа, —
переключатель кабинетов скоро уберут, и ничего не должно на нём держаться.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app.core import access, accounts, board, db, security
from app.main import app
from app.markets.avito import client as avito
from app.markets.avito import sync as avito_sync
from app.markets.ozon import store as ozon_store
from app.markets.yandex import sync as yandex_sync


@pytest.fixture
def cabinets(sample_data, avito_account, yandex_account):
    """Три кабинета трёх площадок с заказами."""
    avito_sync.sync_avito(avito_account)
    yandex_sync.sync_yandex(yandex_account)
    return {"ozon": accounts.default_account(), "avito": avito_account, "yandex": yandex_account}


def enter(test_client, login="admin", password="test-admin-pass"):
    test_client.post("/login", data={"login": login, "password": password, "next": "/pack"})
    page = test_client.get("/pack")
    test_client.headers["X-CSRF-Token"] = re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1)
    return test_client


@pytest.fixture
def client(cabinets):
    with TestClient(app, follow_redirects=False) as test_client:
        yield enter(test_client)


def numbers(page: str) -> list[str]:
    return re.findall(r'data-number="([^"]+)"', page)


def badge(page: str, title: str) -> int:
    found = re.search(rf'{title} <span class="badge">(\d+)</span>', page)
    assert found, f"на странице нет «{title}»"
    return int(found.group(1))


def one_of(table: str, account: dict, where: str, params=()):
    row = db.query_one(f"SELECT * FROM {table} WHERE account_id = ? AND {where} LIMIT 1", (account["id"], *params))
    assert row, f"нет строки {table} {where}"
    return dict(row)


# ------------------------------------------------------------------ статусы
def test_every_market_lands_in_the_three_statuses(cabinets):
    """Статусы площадок раскладываются по трём статусам склада."""
    ozon, avito_shop, yandex = cabinets["ozon"], cabinets["avito"], cabinets["yandex"]
    waiting = {(o["market"], o["id"]) for o in board.rows(board.shops(), "packaging")}
    shipping = {(o["market"], o["id"]) for o in board.rows(board.shops(), "deliver")}

    assert ("ozon", one_of("postings", ozon, "status = 'awaiting_packaging'")["posting_number"]) in waiting
    assert ("ozon", one_of("postings", ozon, "status = 'awaiting_deliver'")["posting_number"]) in shipping
    assert ("avito", one_of("avito_orders", avito_shop, "status = 'on_confirmation'")["id"]) in waiting
    assert ("avito", one_of("avito_orders", avito_shop, "status = 'ready_to_ship'")["id"]) in shipping
    assert ("yandex", one_of("yandex_orders", yandex, "substatus = 'STARTED'")["id"]) in waiting
    assert ("yandex", one_of("yandex_orders", yandex, "substatus = 'READY_TO_SHIP'")["id"]) in shipping

    # Собранное в панели — «Собран», у любой площадки.
    number = one_of("postings", ozon, "status = 'awaiting_deliver'")["posting_number"]
    db.execute("UPDATE postings SET local_state = 'packed', packed_by = 'admin', packed_at = ? "
               "WHERE posting_number = ?", (db.now_iso(), number))
    assert ("ozon", number) in {(o["market"], o["id"]) for o in board.rows(board.shops(), "packed")}
    assert ("ozon", number) not in {(o["market"], o["id"]) for o in board.rows(board.shops(), "deliver")}


def test_returns_are_not_orders(cabinets):
    """Возврат Avito — не заказ в работе: в «Заказах» его нет ни в одном статусе."""
    returned = one_of("avito_orders", cabinets["avito"], "status = ?", (avito.STATUS_ON_RETURN,))["id"]
    for key, _title in board.STATUSES:
        assert returned not in {o["id"] for o in board.rows(board.shops(), key)}


# ------------------------------------------------------------------ страница
def test_all_cabinets_in_one_list(client, cabinets):
    page = client.get("/orders").text
    shops = set(re.findall(r'data-shop-title="([^"]+)"', page))
    assert shops == {"Ozon", "Avito", "Маркет"}, shops
    # Чипы — на каждый кабинет, и «Все заказы» активен.
    assert re.search(r'class="chip active"[^>]*>\s*Все заказы', page)
    for account in cabinets.values():
        assert f"/orders?shop={account['id']}&amp;status=packaging" in page


def test_cabinet_filter_narrows_the_list(client, cabinets):
    avito_shop = cabinets["avito"]
    page = client.get(f"/orders?shop={avito_shop['id']}").text
    assert set(re.findall(r'data-shop-title="([^"]+)"', page)) == {"Avito"}
    # Незнакомый кабинет — «Все заказы», а не пустой экран.
    assert set(re.findall(r'data-shop-title="([^"]+)"', client.get("/orders?shop=999").text)) == {
        "Ozon", "Avito", "Маркет"}


def test_counts_are_crossed(client, cabinets):
    """На кабинете — сколько у него в выбранном статусе; на статусе — сколько в кабинете."""
    avito_shop = cabinets["avito"]
    counts = board.counts(board.shops())
    everyone = sum(n for (_id, key), n in counts.items() if key == "packaging")
    page = client.get("/orders?status=packaging").text
    assert badge(page, "Все заказы") == everyone
    assert badge(page, "Ожидает сборки") == everyone
    assert len(numbers(page)) == everyone

    page = client.get(f"/orders?shop={avito_shop['id']}&status=deliver").text
    assert badge(page, "Ожидает сборки") == counts[(avito_shop["id"], "packaging")]
    assert badge(page, "Ожидает отгрузки") == counts[(avito_shop["id"], "deliver")]
    assert badge(page, "Все заказы") == sum(n for (_id, key), n in counts.items() if key == "deliver")


def test_search_narrows_rows_and_counts(client, cabinets):
    yandex = cabinets["yandex"]
    order = one_of("yandex_orders", yandex, "substatus = 'STARTED'")
    page = client.get(f"/orders?status=packaging&q={order['id']}").text
    assert numbers(page) == [order["id"]]
    assert badge(page, "Все заказы") == 1
    assert "Сбросить" in page


def test_old_tab_names_still_open_their_status(client):
    page = client.get("/orders?tab=deliver").text
    assert re.search(r'class="active">\s*Ожидает отгрузки', page)


def test_nav_has_one_orders_item_with_counts_over_all_cabinets(client, cabinets):
    counts = board.counts(board.shops())
    page = client.get("/logs").text
    assert 'href="/orders"' in page
    for gone in ("Заказы FBS", "Заказы Avito", "Заказы Маркета"):
        assert gone not in page
    waiting = sum(n for (_id, key), n in counts.items() if key == "packaging")
    assert f'title="Ожидает сборки">{waiting}</span>' in page
    # «Заказы» — сразу за «Сборкой», перед «Возвратами».
    nav = page[page.index("<nav>"):page.index("</nav>")]
    assert nav.index("Сборка") < nav.index("Заказы") < nav.index("Возвраты")


def test_bulk_buttons_follow_the_rows_on_screen(client, cabinets):
    """«Собрать в Ozon» — только там, где есть что собирать в Ozon."""
    waiting = client.get("/orders?status=packaging").text
    assert 'data-bulk="ozon:ship"' in waiting
    assert 'data-bulk="avito:confirm"' in waiting
    shipping = client.get(f"/orders?shop={cabinets['ozon']['id']}&status=deliver").text
    assert 'data-bulk="ozon:ship"' not in shipping
    assert 'data-bulk="avito:confirm"' not in shipping
    assert 'data-bulk="labels"' in shipping


def test_a_packer_with_orders_opens_the_section(cabinets):
    db.execute(
        "INSERT INTO users(login, password_hash, role, sections, active, created_at) VALUES(?,?,?,?,1,?)",
        ("sborshik", security.hash_password("parol1234567"), access.PACKER,
         access.dump_sections(access.PACKER, ["pack", "orders"]), db.now_iso()),
    )
    db.execute(
        "INSERT INTO users(login, password_hash, role, sections, active, created_at) VALUES(?,?,?,?,1,?)",
        ("tolko-sborka", security.hash_password("parol1234567"), access.PACKER,
         access.dump_sections(access.PACKER, ["pack"]), db.now_iso()),
    )
    number = one_of("postings", cabinets["ozon"], "status = 'awaiting_deliver'")["posting_number"]
    db.execute("UPDATE postings SET local_state = 'packed', packed_by = 'sborshik', packed_at = ? "
               "WHERE posting_number = ?", (db.now_iso(), number))
    with TestClient(app, follow_redirects=False) as packer:
        enter(packer, "sborshik", "parol1234567")
        page = packer.get("/orders?status=packed")
        assert page.status_code == 200
        assert number in page.text
        # «Снять отметку» — только администратору и владельцу.
        assert "data-reset" not in page.text
        refused = packer.post("/api/orders/reset", json={"account_id": cabinets["ozon"]["id"], "id": number})
        assert refused.status_code == 403
    with TestClient(app, follow_redirects=False) as stranger:
        enter(stranger, "tolko-sborka", "parol1234567")
        assert stranger.get("/orders").status_code == 403
        assert stranger.post("/api/orders/action", json={
            "account_id": cabinets["ozon"]["id"], "action": "ship", "ids": [number]}).status_code == 403


# ------------------------------------------------------------------ действия
def test_ship_in_ozon_goes_to_the_orders_cabinet_not_the_header(client, cabinets):
    """Стоим в Маркете — «Собрать в Ozon» всё равно уходит в кабинет Ozon."""
    number = one_of("postings", cabinets["ozon"], "status = 'awaiting_packaging'")["posting_number"]
    response = client.post("/api/orders/action", json={
        "account_id": cabinets["ozon"]["id"], "action": "ship", "ids": [number]})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"
    assert response.json()["shop"] == "Ozon"
    row = db.query_one("SELECT status FROM postings WHERE posting_number = ?", (number,))
    assert row["status"] == ozon_store.STATUS_AWAITING_DELIVER


def test_actions_check_the_cabinet_and_the_order(client, cabinets):
    ozon, avito_shop = cabinets["ozon"], cabinets["avito"]
    number = one_of("postings", ozon, "status = 'awaiting_packaging'")["posting_number"]

    def act(account_id, action, ids):
        return client.post("/api/orders/action", json={"account_id": account_id, "action": action, "ids": ids})

    # Заказ Ozon, а кабинет назван Avito, — чужой заказ не трогаем.
    assert act(avito_shop["id"], "confirm", [number]).status_code == 404
    # У Ozon нет «Подтвердить», у Маркета нет действий вовсе.
    assert act(ozon["id"], "confirm", [number]).status_code == 400
    assert act(cabinets["yandex"]["id"], "ship", ["1"]).status_code == 400
    assert act(ozon["id"], "ship", []).status_code == 400
    assert act(999, "ship", [number]).status_code == 404
    # Выключенный кабинет — как несуществующий.
    accounts.update(ozon["id"], active=0)
    assert act(ozon["id"], "ship", [number]).status_code == 404
    assert db.query_one("SELECT status FROM postings WHERE posting_number = ?", (number,))["status"] == \
        ozon_store.STATUS_AWAITING_PACKAGING


def test_labels_come_from_the_orders_cabinet(client, cabinets):
    """Наклейки печатаются по кабинету заказа: стоим в Avito — стикер Ozon."""
    number = one_of("postings", cabinets["ozon"], "status = 'awaiting_deliver'")["posting_number"]
    response = client.post("/api/orders/labels.pdf", json={"account_id": cabinets["ozon"]["id"], "ids": [number]})
    assert response.status_code == 200, response.text
    assert response.content.startswith(b"%PDF")
    assert response.headers["X-Page-Size"] == "75x120"
    assert db.query_one("SELECT print_count FROM postings WHERE posting_number = ?", (number,))["print_count"] == 1
    # Чужой номер в кабинете — отказ, а не пустой лист.
    foreign = one_of("avito_orders", cabinets["avito"], "status = 'ready_to_ship'")["id"]
    refused = client.post("/api/orders/labels.pdf", json={"account_id": cabinets["ozon"]["id"], "ids": [foreign]})
    assert refused.status_code == 404


def test_the_owner_can_unmark_a_packed_order(client, cabinets):
    number = one_of("postings", cabinets["ozon"], "status = 'awaiting_deliver'")["posting_number"]
    db.execute("UPDATE postings SET local_state = 'packed', packed_by = 'sborshik', packed_at = ? "
               "WHERE posting_number = ?", (db.now_iso(), number))
    page = client.get("/orders?status=packed").text
    assert "data-reset" in page
    done = client.post("/api/orders/reset", json={"account_id": cabinets["ozon"]["id"], "id": number})
    assert done.status_code == 200, done.text
    row = db.query_one("SELECT local_state, packed_by FROM postings WHERE posting_number = ?", (number,))
    assert row["local_state"] == "new" and row["packed_by"] is None
    assert db.query_one("SELECT 1 FROM events WHERE kind = 'posting_reset'")
    missing = client.post("/api/orders/reset", json={"account_id": cabinets["ozon"]["id"], "id": "нет-такого"})
    assert missing.status_code == 404


def test_sync_updates_the_cabinets_under_the_filter(client, cabinets):
    response = client.post("/api/orders/sync?shop=all")
    assert response.status_code == 200, response.text
    assert sorted(response.json()["updated"]) == sorted(a["title"] for a in cabinets.values())
    one = client.post(f"/api/orders/sync?shop={cabinets['avito']['id']}")
    assert one.json()["updated"] == ["Avito"]
