"""Общий список заказов на «Сборке»: все кабинеты сразу, свой — сверху.

Очередь на рабочем месте показывает только текущий кабинет, и это правильно:
собирают по одному. Но видеть, что горит у соседнего магазина, сборщику нужно
постоянно — раньше для этого переключали кабинеты по очереди.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app.core import accounts, db, orders, sync
from app.main import app
from app.markets.avito import client as avito
from app.markets.avito import sync as avito_sync
from app.markets.yandex import sync as yandex_sync


@pytest.fixture
def cabinets(sample_data):
    """Четыре кабинета на трёх площадках, у каждого свои заказы."""
    second = accounts.get(accounts.create("ozon", "Второй склад", "test-client", "test-key"))
    sync.sync_all()
    avito_one = accounts.get(accounts.create("avito", "Магазин Avito", "test-client", "test-secret"))
    avito_sync.sync_avito(avito_one)
    yandex_one = accounts.get(accounts.create("yandex", "Маркет", "9000001", "test-token"))
    yandex_sync.sync_yandex(yandex_one)
    return {
        "first": accounts.default_account(),
        "second": second,
        "avito": avito_one,
        "yandex": yandex_one,
    }


@pytest.fixture
def client(cabinets):
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/pack"})
        page = test_client.get("/pack")
        test_client.headers["X-CSRF-Token"] = re.search(
            r'name="csrf-token" content="([^"]*)"', page.text
        ).group(1)
        yield test_client


def shops_of(rows):
    """Названия магазинов в порядке, в котором они идут в списке."""
    seen = []
    for row in rows:
        if not seen or seen[-1] != row["shop"]:
            seen.append(row["shop"])
    return seen


def test_every_cabinet_is_in_the_list(cabinets):
    """В списке заказы всех кабинетов, а не только текущего."""
    rows = orders.everywhere(cabinets["first"])
    assert {row["shop"] for row in rows} == {
        cabinets["first"]["title"], "Второй склад", "Магазин Avito", "Маркет",
    }
    assert {row["market"] for row in rows} == {"ozon", "avito", "yandex"}


def test_current_cabinet_goes_first(cabinets):
    """Свои заказы сверху — переключились на другой кабинет, поднялись его."""
    for key in ("first", "second", "avito", "yandex"):
        account = cabinets[key]
        rows = orders.everywhere(account)
        assert rows, "список пуст"
        own = [row for row in rows if row["own"]]
        assert own, f"у кабинета «{account['title']}» нет своих заказов"
        assert rows[: len(own)] == own, "свои заказы не сверху"
        assert shops_of(rows)[0] == account["title"]


def test_other_shops_go_in_order(cabinets):
    """Остальные магазины — по алфавиту: список не должен прыгать между обновлениями."""
    rows = orders.everywhere(cabinets["first"])
    others = shops_of([row for row in rows if not row["own"]])
    assert others == sorted(others, key=str.lower)


def test_inside_a_shop_the_nearest_deadline_is_first(cabinets):
    """Внутри магазина раньше тот, у кого срок ближе."""
    rows = [row for row in orders.everywhere(cabinets["first"]) if row["own"]]
    ranks = [orders.URGENCY_ORDER.get(row["urgency"], 9) for row in rows]
    assert ranks == sorted(ranks), f"порядок срочности: {ranks}"


def test_a_row_says_what_to_collect(cabinets):
    """Строка отвечает на вопросы сборщика: чей заказ, что в нём, к какому сроку."""
    rows = orders.everywhere(cabinets["first"])
    row = next(row for row in rows if row["market"] == "ozon" and row["in_work"])
    assert row["number"] and row["goods"], row
    assert row["quantity"] >= 1
    assert row["status_label"]
    assert row["urgency"] in orders.URGENCY_ORDER


def test_packed_orders_stay_but_leave_the_work(cabinets):
    """Собранный заказ из списка не исчезает, но «в работе» больше не считается."""
    account = cabinets["first"]
    number = db.query_one(
        "SELECT posting_number FROM postings WHERE account_id = ? AND local_state != 'packed' LIMIT 1",
        (account["id"],),
    )["posting_number"]
    db.execute(
        "UPDATE postings SET local_state = 'packed' WHERE account_id = ? AND posting_number = ?",
        (account["id"], number),
    )
    row = next(row for row in orders.everywhere(account) if row["number"] == number)
    assert row["in_work"] is False
    assert row["status_label"] == "Собрано"


def test_avito_return_is_not_work_for_the_packer(cabinets):
    """Возврат Avito виден в списке, но собирать его не нужно — у него свой раздел."""
    account = cabinets["avito"]
    order = db.query_one(
        "SELECT id, marketplace_id FROM avito_orders WHERE account_id = ? AND status = ? LIMIT 1",
        (account["id"], avito.STATUS_ON_RETURN),
    )
    if not order:
        pytest.skip("в подделке Avito нет возвратов")
    row = next(
        row for row in orders.everywhere(account)
        if row["number"] == (order["marketplace_id"] or order["id"])
    )
    assert row["in_work"] is False
    assert row["status_label"] == "На возврате"


def test_the_list_is_the_same_on_every_workspace(client, cabinets):
    """Список один и тот же на «Сборке» любой площадки — он общий."""
    pages = [
        ("/pack", cabinets["first"]),
        ("/avito/pack", cabinets["avito"]),
        ("/yandex/pack", cabinets["yandex"]),
    ]

    for where, account in pages:
        switched = client.post(
            "/api/account/switch", json={"account_id": account["id"], "next": where}
        )
        assert switched.status_code == 200, switched.text
        page = client.get(where)
        assert page.status_code == 200, page.text
        assert "Все заказы" in page.text
        # На странице есть заказы чужих кабинетов — ради этого список и заведён.
        foreign = next(row for row in orders.everywhere(account) if not row["own"])
        assert str(foreign["number"]) in page.text, where
        assert foreign["shop"] in page.text, where


def test_history_keeps_three_scans(client):
    """«Последние сканы» укорочены до трёх — список заказов важнее длинной ленты."""
    page = client.get("/pack")
    assert "показываются три последних" in page.text
