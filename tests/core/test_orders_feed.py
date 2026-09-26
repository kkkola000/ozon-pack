"""Общий список заказов на «Сборке»: все кабинеты одной очередью.

Это очередь склада, а не выписка по кабинету: сборщик стоит у одного стола и
берёт заказы всех площадок подряд. Порядок сквозной — сначала просроченное,
потом горящее. Магазин в сортировке не участвует: кабинет в шапке ничего не
решает, и делить список на «мою работу» и «чужую» больше не по чему.
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
    rows = orders.everywhere()
    assert {row["shop"] for row in rows} == {
        cabinets["first"]["title"], "Второй склад", "Магазин Avito", "Маркет",
    }
    assert {row["market"] for row in rows} == {"ozon", "avito", "yandex"}


def test_the_list_is_one_queue_by_urgency(cabinets):
    """Сначала просроченное, потом горящее — независимо от магазина."""
    rows = orders.everywhere()
    ranks = [orders.URGENCY_ORDER.get(row["urgency"], 9) for row in rows]
    assert ranks == sorted(ranks), f"порядок срочности: {ranks}"
    deadlines = [(r["urgency"], r["deadline"] or "") for r in rows]
    assert deadlines == sorted(deadlines, key=lambda d: (orders.URGENCY_ORDER.get(d[0], 9), d[1]))


def test_shops_are_mixed_together(cabinets):
    """Магазины идут вперемешку: очередь одна, и кабинет в ней ничего не значит.

    Раньше список был сгруппирован — свой кабинет сверху, остальные по
    алфавиту. Это осталось от времён, когда сборка шла в одном кабинете.
    """
    rows = orders.everywhere()
    assert len(shops_of(rows)) > len({row["shop"] for row in rows}), \
        "список всё ещё сгруппирован по магазинам"


def test_the_list_does_not_depend_on_the_open_cabinet(cabinets):
    """Список один и тот же, какой бы кабинет ни был открыт в шапке."""
    assert "own" not in orders.everywhere()[0], "признак «свой кабинет» больше не нужен"


def test_a_row_says_what_to_collect(cabinets):
    """Строка отвечает на вопросы сборщика: чей заказ, что в нём, к какому сроку."""
    rows = orders.everywhere()
    row = next(row for row in rows if row["market"] == "ozon" and row["in_work"])
    assert row["number"] and row["goods"], row
    assert row["quantity"] >= 1
    assert row["status_label"]
    assert row["urgency"] in orders.URGENCY_ORDER


def test_packed_orders_stay_but_leave_the_work(cabinets):
    """Собранный заказ из списка не исчезает, но «в работе» больше не считается."""
    account = cabinets["first"]
    number = db.query_one(
        # Собирают в панели отправление «Ожидает отгрузки» — его и помечаем.
        "SELECT posting_number FROM postings WHERE account_id = ? AND local_state != 'packed' "
        "AND status = 'awaiting_deliver' LIMIT 1",
        (account["id"],),
    )["posting_number"]
    db.execute(
        "UPDATE postings SET local_state = 'packed' WHERE account_id = ? AND posting_number = ?",
        (account["id"], number),
    )
    row = next(row for row in orders.everywhere() if row["number"] == number)
    assert row["in_work"] is False
    assert row["status_label"] == "Собран"


def test_avito_return_is_not_work_for_the_packer(cabinets):
    """Возврат Avito — не заказ: в списке «Сборки» его нет, он в разделе «Возвраты».

    Список строится из тех же статусов склада, что и «Заказы», а возврат ни в
    один из них не входит.
    """
    account = cabinets["avito"]
    order = db.query_one(
        "SELECT id, marketplace_id FROM avito_orders WHERE account_id = ? AND status = ? LIMIT 1",
        (account["id"], avito.STATUS_ON_RETURN),
    )
    if not order:
        pytest.skip("в подделке Avito нет возвратов")
    numbers = {row["number"] for row in orders.everywhere()}
    assert (order["marketplace_id"] or order["id"]) not in numbers


def test_the_list_has_every_cabinet(client, cabinets):
    """На «Сборке» — заказы всех кабинетов: список общий."""
    page = client.get("/pack")
    assert page.status_code == 200, page.text
    assert "Все заказы" in page.text
    for account in (cabinets["first"], cabinets["avito"], cabinets["yandex"]):
        mine = next(row for row in orders.everywhere() if row["account_id"] == account["id"])
        assert str(mine["number"]) in page.text, account["title"]
        assert mine["shop"] in page.text, account["title"]


def test_history_keeps_three_scans():
    """«Последние сканы» укорочены до трёх — список заказов важнее длинной ленты.

    Обрезает ленту общее рабочее место, одно на все площадки, поэтому и
    проверяем его: подписи на странице про это больше нет.
    """
    from app.core.config import BASE_DIR

    script = (BASE_DIR / "app" / "static" / "market_pack.js").read_text(encoding="utf-8")
    assert "if (history.length > 3) history.pop();" in script
