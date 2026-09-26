"""Заказы Avito: загрузка, подтверждение, отправка, этикетки.

Проверяем главное требование: сборщик видит только рабочие статусы —
«Подтвердите заказ» и «Отправьте заказ» (собранное из второго выносится на
отдельную вкладку), а всё остальное панель не показывает и не хранит.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app.core import accounts, db, return_acts, store, sync
from app.markets.avito import client as avito
from app.main import app
from tests import fakes
from app.markets.avito import sync as avito_sync
from app.markets.avito import store as avito_store


@pytest.fixture
def avito_account():
    account = accounts.get(accounts.create("avito", "Avito", "test-client", "test-secret"))
    avito_sync.sync_avito(account)
    return account


@pytest.fixture
def client(avito_account):
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/pack"})
        page = test_client.get("/pack")
        test_client.headers["X-CSRF-Token"] = re.search(
            r'name="csrf-token" content="([^"]*)"', page.text
        ).group(1)
        yield test_client


def orders_in(account, status):
    return db.query(
        "SELECT * FROM avito_orders WHERE account_id = ? AND status = ?", (account["id"], status)
    )


def act(client, account, action, ids):
    """Действие из общего раздела «Заказы»: кабинет называет запрос, а не шапка."""
    return client.post("/api/orders/action", json={"account_id": account["id"], "action": action, "ids": ids})


def orders_page(client, account, status="packaging"):
    return client.get(f"/orders?shop={account['id']}&status={status}")


def row_html(page: str, order_id: str) -> str:
    """Строка заказа на странице — от его <tr> до закрывающего."""
    start = page.index(f'data-id="{order_id}"')
    return page[start:page.index("</tr>", start)]


# ------------------------------------------------------------------ синхронизация
def test_only_work_statuses_are_stored(avito_account):
    """В панель попадают только заказы, с которыми сборщику надо что-то сделать."""
    statuses = {row["status"] for row in db.query(
        "SELECT DISTINCT status FROM avito_orders WHERE account_id = ?", (avito_account["id"],)
    )}
    assert statuses
    assert statuses <= set(avito.SYNC_STATUSES), f"лишние статусы в панели: {statuses}"
    for gone in (avito.STATUS_IN_TRANSIT, avito.STATUS_DELIVERED, avito.STATUS_CLOSED, avito.STATUS_CANCELED):
        assert gone not in statuses


def test_buyer_phone_is_not_stored(avito_account):
    """Телефон покупателя нигде не показывается — значит, и в базе ему не место."""
    rows = db.query(
        "SELECT buyer_name, raw FROM avito_orders WHERE account_id = ?", (avito_account["id"],)
    )
    assert rows, "заказы Avito должны загрузиться"
    # Имя остаётся: по нему находят посылку в пункте выдачи.
    assert any(row["buyer_name"] for row in rows)
    assert all("phoneNumber" not in (row["raw"] or "") for row in rows)
    assert "buyer_phone" not in db.full_schema()


def test_order_items_are_saved(avito_account):
    order = orders_in(avito_account, avito.STATUS_ON_CONFIRMATION)[0]
    items = avito_store.avito_items(avito_account["id"], order["id"])
    assert items, "у заказа должен быть состав"
    assert all(item["title"] for item in items)
    assert order["positions_count"] == len(items)


def test_order_leaving_work_status_disappears(avito_account):
    """Заказ уехал в «в пути» — из панели он уходит, сборщику там делать нечего."""
    order = orders_in(avito_account, avito.STATUS_READY_TO_SHIP)[0]
    client = avito.get_client(avito_account)
    client._orders[order["id"]]["status"] = avito.STATUS_IN_TRANSIT

    avito_sync.sync_avito(avito_account)
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
    response = act(client, avito_account, "confirm", [order["id"]])
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
    response = act(client, avito_account, "ship", [order["id"]])
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
    act(client, avito_account, "confirm", [order["id"]])

    # Возвращаем локальный статус, как будто список ещё не обновился.
    db.execute(
        "UPDATE avito_orders SET status = ? WHERE account_id = ? AND id = ?",
        (avito.STATUS_ON_CONFIRMATION, avito_account["id"], order["id"]),
    )
    response = act(client, avito_account, "confirm", [order["id"]])
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert "уже подтверждён" in body["results"][0]["message"]
    assert db.query_one("SELECT COUNT(*) AS c FROM events WHERE kind = 'avito_error'")["c"] == 1


def test_order_from_another_cabinet_is_not_touched(client, avito_account):
    """Чужой заказ не подтвердить: кабинеты изолированы."""
    other = accounts.get(accounts.create("avito", "Второй Avito", "id-2", "secret-2"))
    avito_sync.sync_avito(other)
    foreign = db.query_one(
        "SELECT id FROM avito_orders WHERE account_id = ? LIMIT 1", (other["id"],)
    )["id"]

    response = act(client, avito_account, "confirm", [foreign])
    assert response.status_code == 404


# ------------------------------------------------------------------ этикетки
def test_label_returns_pdf_and_counts_print(client, avito_account):
    order = orders_in(avito_account, avito.STATUS_READY_TO_SHIP)[0]
    response = client.post("/api/orders/labels.pdf", json={"account_id": avito_account["id"], "ids": [order["id"]]})
    assert response.status_code == 200, response.text
    assert response.content[:4] == b"%PDF"
    # Размер листа — по нему браузер выбирает принтер этикеток Avito.
    assert response.headers["X-Page-Size"]

    row = db.query_one(
        "SELECT printed_at, print_count FROM avito_orders WHERE account_id = ? AND id = ?",
        (avito_account["id"], order["id"]),
    )
    assert row["printed_at"] and row["print_count"] == 1


def test_labels_batch_is_limited(client, avito_account):
    response = client.post("/api/orders/labels.pdf",
                           json={"account_id": avito_account["id"], "ids": [str(i) for i in range(51)]})
    assert response.status_code == 400


# ------------------------------------------------------------------ страница
def test_orders_show_avito_work_statuses(client, avito_account):
    """В «Заказах» — только то, с чем сборщику работать, и своим словом Avito.

    «Подтвердите заказ» лежит в «Ожидает сборки», «Отправьте заказ» — в
    «Ожидает отгрузки». Остальные возможности Avito в интерфейс не выводятся.
    """
    waiting = orders_page(client, avito_account, "packaging")
    assert waiting.status_code == 200
    assert "Avito: «Подтвердите заказ»" in waiting.text
    shipping = orders_page(client, avito_account, "deliver")
    assert "Avito: «Отправьте заказ»" in shipping.text
    for page in (waiting, shipping):
        for hidden in ("Отменить заказ", "Честный знак", "Трек-номер", "интервал курьера"):
            assert hidden not in page.text, f"в интерфейсе не должно быть «{hidden}»"


def test_old_avito_address_leads_to_shared_orders(client, avito_account):
    """«Заказы Avito» больше нет: старый адрес ведёт в общий раздел, в тот же статус."""
    for tab, status in (("confirm", "packaging"), ("ship", "deliver"), ("packed", "packed")):
        assert client.get(f"/avito?tab={tab}").headers["location"] == f"/orders?status={status}"
    order = orders_in(avito_account, avito.STATUS_ON_CONFIRMATION)[0]
    assert (order["marketplace_id"] or order["id"]) in orders_page(client, avito_account).text


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
    from app.markets.ozon import client as ozon

    def boom(*args, **kwargs):
        raise ozon.OzonError("нет связи")

    monkeypatch.setattr(fakes.FakeOzonClient, "posting_list", boom)
    result = sync.sync_all()
    assert result.get("avito"), "заказы Avito должны загрузиться несмотря на сбой Ozon"
    assert result.get("errors")


# ------------------------------------------------------------------ возвраты
def returns_of(account):
    return db.query(
        "SELECT * FROM avito_orders WHERE account_id = ? AND status = ?",
        (account["id"], avito.STATUS_ON_RETURN),
    )


def test_only_returns_ready_for_pickup_are_stored(avito_account):
    """Забрать можно только то, что доехало до пункта выдачи — остальное не храним."""
    rows = returns_of(avito_account)
    assert rows, "возвраты не загрузились"
    # Оба написания, которые встречаются у Avito, считаются готовыми к выдаче.
    assert all(avito.is_ready_for_pickup(row["return_status"]) for row in rows)

    # Avito отдал и возвраты в пути — панель их отбросила, но не молча.
    client = avito.get_client(avito_account)
    in_transit = [
        o for o in client._orders.values()
        if o["status"] == avito.STATUS_ON_RETURN
        and (o.get("returnPolicy") or {}).get("returnStatus") == avito.RETURN_IN_TRANSIT
    ]
    assert in_transit, "в демо-данных нет возвратов в пути — проверять нечего"
    for order in in_transit:
        assert db.query_one(
            "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND id = ?",
            (avito_account["id"], order["id"]),
        )["c"] == 0
    # Сколько отбросили — видно в итоге синхронизации, а не в отдельной памяти.
    assert avito_sync.sync_avito(avito_account)["avito_returns_skipped"] >= len(in_transit)


def test_return_that_left_pickup_point_counts_as_received(avito_account):
    """Возврат забрали, Avito перестал его отдавать — значит, он получен.

    Своего статуса «получен» у Avito нет, и это единственный признак: строку
    не удаляем, а помечаем полученной — из полученных за день собирается акт.
    """
    row = returns_of(avito_account)[0]
    client = avito.get_client(avito_account)
    client._orders[row["id"]]["returnPolicy"]["returnStatus"] = avito.RETURN_IN_TRANSIT

    result = avito_sync.sync_avito(avito_account)
    stored = db.query_one(
        "SELECT received_at, received_day FROM avito_orders WHERE account_id = ? AND id = ?",
        (avito_account["id"], row["id"]),
    )
    assert stored and stored["received_at"], "получение не записано"
    assert stored["received_day"] == store.local_day(stored["received_at"])
    assert result.get("avito_received") == 1


def test_page_shows_returns_ready_for_pickup(client, avito_account):
    page = client.get("/returns")
    assert page.status_code == 200
    assert "Заберите заказ" in page.text
    for row in returns_of(avito_account):
        assert (row["marketplace_id"] or row["id"]) in page.text


def test_returns_page_is_read_only(client):
    """Возвраты только читаются: ни фильтров, ни отметок вручную.

    Забранный возврат Avito переводит дальше сам, и синхронизация убирает его
    из панели — ручная отметка была бы вторым источником правды.
    """
    page = client.get("/returns")
    # В шапке остаётся переключатель кабинетов — проверяем именно фильтр списка.
    assert 'name="show"' not in page.text
    assert "Забранные" not in page.text
    assert "Отметить забранными" not in page.text
    assert 'class="pick"' not in page.text
    # Старая ссылка с фильтром не должна ничего ломать.
    assert client.get("/returns?show=taken").status_code == 200


def test_marking_returns_taken_is_gone(client, avito_account):
    row = returns_of(avito_account)[0]
    response = client.post("/api/avito/returns/taken", json={"ids": [row["id"]], "taken": True})
    assert response.status_code == 404, "ручная отметка должна быть убрана целиком"


def test_collected_return_leaves_the_pickup_list(client, avito_account):
    """Кладовщик забрал возврат — Avito меняет статус, и из «К выдаче» он уходит.

    Сама строка остаётся: по ней ещё нужна отметка, и она ждёт акта за своё
    число. Забирать по ней больше нечего — в списке к выдаче её нет.
    """
    row = returns_of(avito_account)[0]
    client_api = avito.get_client(avito_account)
    client_api._orders[row["id"]]["status"] = avito.STATUS_CLOSED

    avito_sync.sync_avito(avito_account)
    assert db.query_one(
        "SELECT received_at FROM avito_orders WHERE account_id = ? AND id = ?",
        (avito_account["id"], row["id"]),
    )["received_at"], "получение не записано"
    assert (row["marketplace_id"] or row["id"]) not in client.get("/returns").text


def test_received_return_becomes_an_act(client, avito_account):
    """Полный круг возврата Avito — тот же, что у Ozon: забрали, акт, отметка, подпись.

    Раньше возврат Avito просто исчезал из панели: акта по нему не выходило,
    и принимать было нечего.
    """
    row = returns_of(avito_account)[0]
    api = avito.get_client(avito_account)
    api._orders[row["id"]]["status"] = avito.STATUS_CLOSED
    avito_sync.sync_avito(avito_account)

    day = db.query_one(
        "SELECT received_day FROM avito_orders WHERE account_id = ? AND id = ?",
        (avito_account["id"], row["id"]),
    )["received_day"]

    # Сколько попадёт в акт, панель считает до его составления — как у Ozon.
    preview = client.post("/api/returns/acts/by-day", json={"day": day, "dry_run": True})
    assert preview.status_code == 200, preview.text
    assert preview.json()["found"] >= 1

    made = client.post("/api/returns/acts/by-day", json={"day": day})
    assert made.status_code == 200, made.text
    act_id = made.json()["act_id"]
    assert act_id, made.text

    act = return_acts.detail(act_id)
    assert row["id"] in {item["id"] for item in act["avito"]}
    assert act["can_confirm"] is False, "акт без отметок подтверждать нельзя"

    # Отметка сборщика и подпись под актом — теми же общими кнопками.
    marked = client.post(
        "/api/returns/mark",
        json={"marketplace": "avito", "id": row["id"], "mark": "ok", "note": "коробка цела"},
    )
    assert marked.status_code == 200, marked.text
    assert return_acts.detail(act_id)["can_confirm"] is True

    confirmed = client.post(f"/api/returns/acts/{act_id}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert return_acts.get(act_id)["confirmed_at"]


def test_received_return_is_claimed_once(client, avito_account):
    """Возврат, попавший в акт, во второй акт не возьмут — иначе две отметки на одну работу."""
    row = returns_of(avito_account)[0]
    api = avito.get_client(avito_account)
    api._orders[row["id"]]["status"] = avito.STATUS_CLOSED
    avito_sync.sync_avito(avito_account)
    day = db.query_one(
        "SELECT received_day FROM avito_orders WHERE account_id = ? AND id = ?",
        (avito_account["id"], row["id"]),
    )["received_day"]

    first = client.post("/api/returns/acts/by-day", json={"day": day}).json()
    assert first["act_id"]
    second = client.post("/api/returns/acts/by-day", json={"day": day}).json()
    assert second["act_id"] is None, "тот же возврат попал во второй акт"

    # И повторная синхронизация второй раз получение не записывает.
    before = db.query_one(
        "SELECT received_at FROM avito_orders WHERE account_id = ? AND id = ?",
        (avito_account["id"], row["id"]),
    )["received_at"]
    avito_sync.sync_avito(avito_account)
    after = db.query_one(
        "SELECT received_at FROM avito_orders WHERE account_id = ? AND id = ?",
        (avito_account["id"], row["id"]),
    )["received_at"]
    assert after == before


def test_returns_print_sheet(client, avito_account):
    page = client.get("/returns/print")
    assert page.status_code == 200
    assert "Возвраты к выдаче" in page.text
    assert "Принял (сборщик)" in page.text
    assert db.query_one("SELECT COUNT(*) AS c FROM events WHERE kind = 'returns_print'")["c"] == 1


def test_print_sheet_shows_pickup_address_without_status(client, avito_account):
    """На бумаге нужен адрес ПВЗ, а не состояние возврата."""
    row = returns_of(avito_account)[0]
    db.execute(
        "UPDATE avito_orders SET terminal_address = ?, service_name = ? WHERE account_id = ? AND id = ?",
        ("Москва, Настасьинский пер., 8с2", "Boxberry", avito_account["id"], row["id"]),
    )
    page = client.get("/returns/print")
    assert "Пункт выдачи" in page.text
    assert "Москва, Настасьинский пер., 8с2" in page.text
    assert "Boxberry" in page.text
    # Столбца со статусом возврата на листе быть не должно.
    assert "Заберите заказ" not in page.text


def test_returns_section_follows_the_cabinet_filter(client, avito_account):
    """Раздел возвратов один: «Все кабинеты» — и Ozon, и Avito; выбран Ozon — без Avito."""
    ozon_account = accounts.all_accounts()[0]
    everything = client.get("/returns").text
    for row in returns_of(avito_account):
        assert (row["marketplace_id"] or row["id"]) in everything
    page = client.get(f"/returns?shop={ozon_account['id']}")
    assert page.status_code == 200
    for row in returns_of(avito_account):
        assert (row["marketplace_id"] or row["id"]) not in page.text


def test_old_order_returned_today_is_not_lost(avito_account):
    """Покупку сделали давно, вернули сейчас — возврат обязан попасть в панель.

    dateFrom у Avito отсекает по дате создания заказа, поэтому окно
    AVITO_DAYS_BACK к возвратам применять нельзя.
    """
    from datetime import datetime, timedelta, timezone

    from app.core.config import settings

    client = avito.get_client(avito_account)
    returns = [o for o in client._orders.values() if o["status"] == avito.STATUS_ON_RETURN]
    assert returns, "в демо-данных нет возвратов"

    long_ago = datetime.now(timezone.utc) - timedelta(days=settings.avito_days_back + 60)
    for order in returns:
        order["createdAt"] = long_ago.isoformat().replace("+00:00", "Z")
        order["returnPolicy"] = {"returnStatus": avito.RETURN_READY, "trackingNumber": "RT-OLD"}

    avito_sync.sync_avito(avito_account)
    stored = db.query_one(
        "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND status = ?",
        (avito_account["id"], avito.STATUS_ON_RETURN),
    )["c"]
    assert stored == len(returns), "старые заказы с возвратом потерялись"


def test_returns_are_requested_without_creation_window(avito_account, monkeypatch):
    """Возвраты запрашиваются отдельно и без dateFrom — иначе они теряются."""
    calls = []

    def fake_orders_all(self, *, statuses=None, date_from=None, max_pages=50):
        calls.append({"statuses": list(statuses or []), "date_from": date_from})
        return []

    monkeypatch.setattr(fakes.FakeAvitoClient, "orders_all", fake_orders_all)
    avito_sync.sync_avito(avito_account)

    assert len(calls) == 2, "рабочие статусы и возвраты должны запрашиваться отдельно"
    work, returns = calls
    assert set(work["statuses"]) == set(avito.WORK_STATUSES)
    assert work["date_from"] is not None
    assert set(returns["statuses"]) == set(avito.RETURN_STATUSES)
    assert returns["date_from"] is None


def test_live_api_spelling_ready_for_pickup_is_accepted(avito_account):
    """Боевой Avito отдаёт ready_for_pickup, схема обещает ready_to_pickup.

    Панель обязана принимать оба написания: иначе возвраты, которые можно
    забрать, молча отбрасываются.
    """
    client = avito.get_client(avito_account)
    returns = [o for o in client._orders.values() if o["status"] == avito.STATUS_ON_RETURN]
    assert returns
    for order in returns:
        order["returnPolicy"] = {"returnStatus": "ready_for_pickup", "trackingNumber": "RT-LIVE"}

    avito_sync.sync_avito(avito_account)
    stored = db.query(
        "SELECT return_status FROM avito_orders WHERE account_id = ? AND status = ?",
        (avito_account["id"], avito.STATUS_ON_RETURN),
    )
    assert len(stored) == len(returns), "возвраты с написанием ready_for_pickup потерялись"
    assert {row["return_status"] for row in stored} == {"ready_for_pickup"}

    # И подпись у них человеческая, а не сырой код из API.
    view = avito_store.avito_view(db.query_one(
        "SELECT * FROM avito_orders WHERE account_id = ? AND status = ? LIMIT 1",
        (avito_account["id"], avito.STATUS_ON_RETURN),
    ))
    assert view["return_label"] == "Заберите заказ"


def test_both_spellings_counted_as_ready():
    assert avito.is_ready_for_pickup("ready_for_pickup")
    assert avito.is_ready_for_pickup("ready_to_pickup")
    assert not avito.is_ready_for_pickup("in_transit")
    assert not avito.is_ready_for_pickup(None)


# ------------------------------------------------------------------ адрес ПВЗ
def test_pickup_address_from_terminal_info():
    raw = {"delivery": {"terminalInfo": {"address": "Москва, Настасьинский пер., 8с2", "code": "MSK14"}}}
    assert avito.pickup_address(raw) == "Москва, Настасьинский пер., 8с2"
    assert avito.pickup_code(raw) == "MSK14"


def test_pickup_address_from_unexpected_place():
    """Схема Avito неполная — адрес ищем и там, где его не обещали."""
    raw = {"delivery": {"serviceName": "Boxberry", "pickupPoint": {"address": "Москва, Кибальчича, 2к1"}}}
    assert avito.pickup_address(raw) == "Москва, Кибальчича, 2к1"

    deep = {"returnPolicy": {"pvzInfo": {"fullAddress": "Казань, Баумана, 1"}}}
    assert avito.pickup_address(deep) == "Казань, Баумана, 1"


def test_pickup_address_absent():
    raw = {"delivery": {"serviceName": "Boxberry", "serviceType": "pvz"}}
    assert avito.pickup_address(raw) is None
    assert avito.pickup_code(raw) is None


def test_address_is_saved_from_any_shape(avito_account):
    """Адрес попадает в базу независимо от того, где Avito его положил."""
    client = avito.get_client(avito_account)
    order = [o for o in client._orders.values() if o["status"] == avito.STATUS_ON_RETURN][0]
    order["delivery"].pop("terminalInfo", None)
    order["delivery"]["pickupPoint"] = {"address": "Санкт-Петербург, Невский пр., 100", "code": "SPB7"}

    avito_sync.sync_avito(avito_account)
    row = db.query_one(
        "SELECT terminal_address, terminal_code FROM avito_orders WHERE account_id = ? AND id = ?",
        (avito_account["id"], order["id"]),
    )
    assert row["terminal_address"] == "Санкт-Петербург, Невский пр., 100"
    assert row["terminal_code"] == "SPB7"


def test_print_sheet_falls_back_to_pvz_code(client, avito_account):
    """Адреса нет — печатаем код ПВЗ, а не пустую клетку."""
    row = returns_of(avito_account)[0]
    db.execute(
        "UPDATE avito_orders SET terminal_address = NULL, terminal_code = 'MSK14' "
        "WHERE account_id = ? AND id = ?",
        (avito_account["id"], row["id"]),
    )
    page = client.get("/returns/print")
    assert "ПВЗ MSK14" in page.text


def test_raw_answer_is_available_to_admin(client, avito_account):
    """Если чего-то не хватает — видно, что именно прислал Avito."""
    row = returns_of(avito_account)[0]
    response = client.get(f"/api/avito/returns/{row['id']}/raw")
    assert response.status_code == 200
    body = response.json()
    assert body["raw"]["id"] == row["id"]
    assert "delivery" in body["raw"]
    assert "pickup_address" in body


# ------------------------------------------------- статус «Собран» в «Заказах»
def test_packed_orders_move_to_their_own_tab(client, avito_account):
    """Собранный заказ уходит из «Ожидает отгрузки» в «Собран».

    Avito о сборке не знает: для площадки заказ всё ещё «Отправьте заказ».
    Собрали мы у себя, и без деления собранное лежало бы вперемешку с
    несобранным — по списку не видно, сколько работы осталось.
    """
    target = db.query_one(
        "SELECT id FROM avito_orders WHERE account_id = ? AND status = ? LIMIT 1",
        (avito_account["id"], avito.STATUS_READY_TO_SHIP),
    )["id"]

    ship = orders_page(client, avito_account, "deliver")
    assert target in ship.text
    assert orders_page(client, avito_account, "packed").text.count(target) == 0

    db.execute("UPDATE avito_orders SET local_state = 'packed' WHERE id = ?", (target,))

    assert target not in orders_page(client, avito_account, "deliver").text, "собранный остался в «Ожидает отгрузки»"
    assert target in orders_page(client, avito_account, "packed").text, "собранного нет в статусе «Собран»"


def test_the_packed_tab_is_offered(client, avito_account):
    """Статус «Собран» есть на экране — иначе о нём никто не узнает."""
    page = orders_page(client, avito_account)
    assert page.status_code == 200
    assert "Собран" in page.text
    assert f"/orders?shop={avito_account['id']}&status=packed" in page.text


def tab_badge(page: str, title: str) -> int:
    """Число на вкладке, как его видит человек."""
    found = re.search(rf'{title} <span class="badge">(\d+)</span>', page)
    assert found, f"на странице нет вкладки «{title}»"
    return int(found.group(1))


def test_packed_orders_leave_the_shipping_count(client, avito_account):
    """Счётчики — это сколько работы осталось, а не сколько заказов вообще.

    Собранный уходит из числа на «Отправьте заказ» и появляется на
    «Собранных» — иначе по вкладкам не видно, что уже сделано.
    """
    target = db.query_one(
        "SELECT id FROM avito_orders WHERE account_id = ? AND status = ? LIMIT 1",
        (avito_account["id"], avito.STATUS_READY_TO_SHIP),
    )["id"]
    before = orders_page(client, avito_account).text
    ship_before = tab_badge(before, "Ожидает отгрузки")
    assert tab_badge(before, "Собран") == 0

    db.execute("UPDATE avito_orders SET local_state = 'packed' WHERE id = ?", (target,))
    after = orders_page(client, avito_account).text

    assert tab_badge(after, "Ожидает отгрузки") == ship_before - 1
    assert tab_badge(after, "Собран") == 1


def test_shipping_still_works_from_the_packed_tab(client, avito_account):
    """Отправку подтверждают из «Собран» — кнопка там та же, в строке и над списком."""
    target = next(
        row["id"] for row in orders_in(avito_account, avito.STATUS_READY_TO_SHIP)
        if "perform" in (row["actions"] or "")
    )
    db.execute("UPDATE avito_orders SET local_state = 'packed' WHERE id = ?", (target,))

    page = orders_page(client, avito_account, "packed").text
    assert 'data-bulk="avito:ship"' in page, "в статусе «Собран» нечем отправить"
    assert 'data-act="avito:ship"' in row_html(page, target)
    assert 'data-bulk="avito:confirm"' not in page


def test_only_a_manager_can_unmark_a_packed_order(client, avito_account):
    """Снять отметку «собрано» может админ или владелец, но не сборщик.

    Отметка — результат работы сборщика, и отвечает за неё тот, кто отвечает за
    склад. Иначе ошибившийся сам же и заметает след.
    """
    from app.core import access, security

    target = db.query_one(
        "SELECT id FROM avito_orders WHERE account_id = ? AND status = ? LIMIT 1",
        (avito_account["id"], avito.STATUS_READY_TO_SHIP),
    )["id"]
    db.execute(
        "UPDATE avito_orders SET local_state = 'packed', packed_by = 'sborshik' WHERE id = ?", (target,))

    # Владелец видит кнопку и снимает отметку.
    page = orders_page(client, avito_account, "packed")
    assert "data-reset" in row_html(page.text, target), "владельцу не показали «Снять отметку»"
    done = client.post("/api/orders/reset", json={"account_id": avito_account["id"], "id": target})
    assert done.status_code == 200, done.text
    row = db.query_one("SELECT local_state, packed_by FROM avito_orders WHERE id = ?", (target,))
    assert row["local_state"] == "new" and row["packed_by"] is None

    # Сборщику кнопку не показывают и запрос от него не принимают.
    db.execute("UPDATE avito_orders SET local_state = 'packed' WHERE id = ?", (target,))
    db.execute(
        "INSERT INTO users(login, password_hash, role, sections, active, created_at) VALUES(?,?,?,?,1,?)",
        ("sborshik", security.hash_password("parol1234567"), access.PACKER,
         access.dump_sections(access.PACKER, ["pack", "orders"]), db.now_iso()),
    )
    with TestClient(app, follow_redirects=False) as packer:
        packer.post("/login", data={"login": "sborshik", "password": "parol1234567", "next": "/pack"})
        csrf = re.search(
            r'name="csrf-token" content="([^"]*)"', packer.get("/pack").text).group(1)
        packer.post("/api/account/switch", json={"account_id": avito_account["id"], "next": "/avito"},
                    headers={"X-CSRF-Token": csrf})
        page = orders_page(packer, avito_account, "packed")
        assert page.status_code == 200, page.text
        assert target in page.text
        assert "data-reset" not in page.text, "сборщику показали «Снять отметку»"
        csrf = re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1)
        refused = packer.post("/api/orders/reset", json={"account_id": avito_account["id"], "id": target},
                              headers={"X-CSRF-Token": csrf})
        assert refused.status_code == 403, refused.text

    assert db.query_one("SELECT local_state FROM avito_orders WHERE id = ?", (target,))["local_state"] == "packed"


def test_the_packed_tab_shows_who_packed_and_when(client, avito_account):
    """Кто собрал и когда — площадка об этом не знает, спросить негде."""
    target = db.query_one(
        "SELECT id FROM avito_orders WHERE account_id = ? AND status = ? LIMIT 1",
        (avito_account["id"], avito.STATUS_READY_TO_SHIP),
    )["id"]
    db.execute(
        "UPDATE avito_orders SET local_state = 'packed', packed_by = 'sborshik', packed_at = ? "
        "WHERE id = ?", (db.now_iso(), target))

    page = orders_page(client, avito_account, "packed")
    assert "Собран" in page.text
    assert "Собрал: sborshik" in page.text
    assert "Статус" in page.text, "в таблице нет колонки статуса"
