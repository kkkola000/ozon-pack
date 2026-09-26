"""Сборка идёт по фильтру, а не по кабинету в шапке.

Ради чего всё: сборщик стоит у одного стола. В руках этикетка Avito, а в шапке
открыт кабинет Ozon — и панель отвечала «такого отправления нет», хотя заказ
есть и горит. Кабинета на складе не существует.

Теперь границы сборки задаёт фильтр площадок: «Все заказы» — сканируется всё, в
каком бы кабинете заказ ни лежал; выбрана площадка — сборка в её границах, и
наклейка чужого кабинета честно не открывается.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app.core import accounts, db, packing, sync
from app.main import app
from app.markets.avito import sync as avito_sync
from app.markets.ozon import store as ozon_store
from app.markets.yandex import sync as yandex_sync


@pytest.fixture
def cabinets(sample_data):
    """Три площадки: Ozon (он же в шапке), Avito и Маркет."""
    sync.sync_all()
    avito_one = accounts.get(accounts.create("avito", "Магазин Avito", "test-client", "test-secret"))
    avito_sync.sync_avito(avito_one)
    yandex_one = accounts.get(accounts.create("yandex", "Маркет", "9000001", "test-token"))
    yandex_sync.sync_yandex(yandex_one)
    return {"ozon": accounts.default_account(), "avito": avito_one, "yandex": yandex_one}


@pytest.fixture
def client(cabinets):
    """Вошли админом; в шапке — кабинет Ozon. Наклейки выгружены, замок снят."""
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/pack"})
        page = test_client.get("/pack")
        test_client.headers["X-CSRF-Token"] = re.search(
            r'name="csrf-token" content="([^"]*)"', page.text).group(1)
        test_client.post("/api/pack/labels.zip")
        yield test_client


def avito_label(cabinets) -> str:
    """Номер, которым открывается заказ Avito: он и напечатан на этикетке."""
    row = db.query_one(
        "SELECT marketplace_id, id FROM avito_orders WHERE account_id = ? AND status = 'ready_to_ship' "
        "AND local_state != 'packed' LIMIT 1",
        (cabinets["avito"]["id"],),
    )
    assert row, "в подделке Avito нет заказов к сборке"
    return row["marketplace_id"] or row["id"]


def ozon_barcode(cabinets) -> tuple[str, str]:
    """Штрихкод товара из отправления Ozon, которое ждёт отгрузки: (штрихкод, sku)."""
    row = db.query_one(
        """SELECT pb.barcode, pb.sku FROM product_barcodes pb
           JOIN posting_items i ON i.sku = pb.sku AND i.account_id = pb.account_id
           JOIN postings p ON p.posting_number = i.posting_number AND p.account_id = i.account_id
           WHERE p.account_id = ? AND p.status = ? AND p.local_state = 'new' LIMIT 1""",
        (cabinets["ozon"]["id"], ozon_store.STATUS_AWAITING_DELIVER),
    )
    assert row, "в подделке Ozon нет товара со штрихкодом"
    return row["barcode"], row["sku"]


def scan(client, code: str, shop="all"):
    """Скан под фильтром: «all» или номер кабинета, как в адресе страницы."""
    response = client.post(f"/api/pack/scan?shop={shop}", json={"code": code})
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------- главное: чужой кабинет
def test_an_avito_label_opens_from_an_ozon_cabinet(client, cabinets):
    """В шапке Ozon, в руках этикетка Avito — заказ открывается.

    Это ровно та поломка, ради которой раздел и объединяли: «такого отправления
    нет» вместо сборки.
    """
    result = scan(client, avito_label(cabinets))

    assert result["status"] == "ok", result["message"]
    assert result["market"] == "avito"
    assert result["shop"] == "Магазин Avito"
    assert result["state"]["active"] is not None
    # Площадка нужна и внутри состояния: по ней браузер выбирает, чем рисовать
    # карточку. Без этого сборка открывалась, а экран оставался пустым.
    assert result["state"]["market"] == "avito"


def test_state_says_whose_order_is_open(client, cabinets):
    """Состояние называет площадку открытого заказа — по ней рисуется карточка."""
    scan(client, avito_label(cabinets))
    state = client.get("/api/pack/state").json()["state"]
    assert state["market"] == "avito"
    assert state["shop"] == "Магазин Avito"


# ------------------------------------------------------------------ фильтр
def test_a_filter_keeps_the_scan_inside_it(client, cabinets):
    """Выбран кабинет Ozon — этикетка Avito не открывается, и это правильный ответ."""
    result = scan(client, avito_label(cabinets), shop=cabinets["ozon"]["id"])

    assert result["status"] == "error"
    assert "не найден" in result["message"]
    assert result["state"]["active"] is None
    assert packing.started({"id": 1}) is None, "сборка всё-таки открылась"


def test_the_same_label_opens_under_its_own_filter(client, cabinets):
    """Тот же скан под кабинетом Avito открывает заказ — границы задаёт фильтр."""
    result = scan(client, avito_label(cabinets), shop=cabinets["avito"]["id"])
    assert result["status"] == "ok", result["message"]
    assert result["market"] == "avito"


# ------------------------------------------------------- начатая сборка сильнее
def test_the_open_assembly_survives_a_filter_change(client, cabinets, user):
    """Коробка в руках важнее фильтра: переключили чип — сборка осталась.

    Начатая сборка у человека одна. Потерять её из-за нажатия на чип значит
    бросить наполовину собранный заказ.
    """
    scan(client, avito_label(cabinets))
    assert packing.started(user)["id"] == cabinets["avito"]["id"]

    state = client.get(f"/api/pack/state?shop={cabinets['ozon']['id']}").json()["state"]
    assert state["market"] == "avito", "сборка пропала при смене фильтра"
    assert state["active"] is not None


def test_a_stranger_label_is_a_stop(client, cabinets):
    """Посреди сборки Avito отсканировали стикер Ozon — стоп, и сказано чей он."""
    scan(client, avito_label(cabinets))
    number = db.query_one(
        "SELECT posting_number FROM postings WHERE account_id = ? AND status = ? LIMIT 1",
        (cabinets["ozon"]["id"], ozon_store.STATUS_AWAITING_DELIVER),
    )["posting_number"]

    result = scan(client, number)
    assert result["status"] == "error"
    assert result["action"] == "wrong_shop"
    assert number in result["message"] and "Ozon" in result["message"]
    # Сборку не бросили: она там же, где была, и рисуется своей площадкой.
    assert result["state"]["active"] is not None
    assert result["state"]["market"] == "avito"
    assert db.query_one("SELECT 1 FROM events WHERE kind = 'scan_wrong_shop'")


def test_a_product_of_another_shop_is_not_a_stop(client, cabinets):
    """Товар соседнего магазина — не стоп: один и тот же товар продаётся везде.

    Стоп только на наклейку: она принадлежит одному заказу. Товар при открытой
    сборке — обычная позиция, её и записываем.
    """
    scan(client, avito_label(cabinets))
    barcode, _sku = ozon_barcode(cabinets)

    result = scan(client, barcode)
    assert result["action"] != "wrong_shop"
    assert result["market"] == "avito", "скан ушёл не в тот кабинет"


# --------------------------------------------------- одинаковый товар в двух кабинетах
def test_the_same_product_opens_the_first_order_in_the_list(client, cabinets, user):
    """Товар нужен в двух кабинетах — открывается тот заказ, что выше в списке.

    Список отсортирован по сроку отгрузки, поэтому «первый по списку» — это и
    есть «горит раньше». Проверяем в обе стороны: срок решает, а не кабинет.
    """
    barcode, sku = ozon_barcode(cabinets)
    second = accounts.get(accounts.create("ozon", "Второй склад", "test-client", "test-key"))
    mine = db.query_one(
        "SELECT p.posting_number AS posting_number FROM postings p JOIN posting_items i "
        "ON i.posting_number = p.posting_number AND i.account_id = p.account_id "
        "WHERE p.account_id = ? AND i.sku = ? AND p.status = ? AND p.local_state = 'new' "
        "ORDER BY p.shipment_date LIMIT 1",
        (cabinets["ozon"]["id"], sku, ozon_store.STATUS_AWAITING_DELIVER),
    )

    def put(shipment_date: str) -> str:
        """Такой же товар во втором кабинете, со своим сроком отгрузки."""
        number = "49000000-9001-1"
        db.execute("DELETE FROM postings WHERE account_id = ?", (second["id"],))
        db.execute("DELETE FROM posting_items WHERE account_id = ?", (second["id"],))
        db.execute(
            "INSERT INTO postings(account_id, posting_number, status, shipment_date, local_state, "
            "label_saved_at, positions_count, items_count) VALUES(?, ?, ?, ?, 'new', ?, 1, 1)",
            (second["id"], number, ozon_store.STATUS_AWAITING_DELIVER, shipment_date, db.now_iso()),
        )
        db.execute(
            "INSERT INTO posting_items(account_id, posting_number, sku, name, quantity) "
            "VALUES(?, ?, ?, 'Тот же товар', 1)",
            (second["id"], number, sku),
        )
        db.execute(
            "INSERT OR REPLACE INTO product_barcodes(account_id, sku, barcode) VALUES(?, ?, ?)",
            (second["id"], sku, barcode),
        )
        return number

    # Второй кабинет горит раньше — берём его заказ.
    earlier = put("2020-01-01T00:00:00+00:00")
    result = scan(client, barcode)
    assert result["status"] == "ok", result["message"]
    assert result["state"]["active"]["posting_number"] == earlier
    assert result["shop"] == "Второй склад"

    client.post("/api/pack/release")
    # А теперь позже — и наверх поднимается заказ первого кабинета.
    put("2099-01-01T00:00:00+00:00")
    result = scan(client, barcode)
    assert result["status"] == "ok", result["message"]
    assert result["state"]["active"]["posting_number"] == mine["posting_number"]


# ------------------------------------- ответ не зависит от кабинета в шапке
RUCKSACK = "8885020503531"


@pytest.fixture
def rucksack(cabinets):
    """Сценарий со склада: товар есть в каталогах двух кабинетов Ozon.

    В кабинете «МК» с ним лежит отправление, но оно ещё «Ожидает сборки». В
    кабинете «Ozon» заказов с этим товаром нет вовсе. Правильный ответ — «ещё
    в статусе «Ожидает сборки»», и он не должен зависеть от того, какой
    кабинет открыт в шапке.
    """
    mk = accounts.get(accounts.create("ozon", "МК", "test-client", "test-key"))
    for shop in (cabinets["ozon"], mk):
        db.execute(
            "INSERT INTO products(account_id, sku, offer_id, name) VALUES(?, 'SKU-ANEX', 'ANEX-1', ?)",
            (shop["id"], "Эргорюкзак Anex Whiz-Hug Lownight"),
        )
        db.execute(
            "INSERT INTO product_barcodes(account_id, barcode, sku) VALUES(?, ?, 'SKU-ANEX')",
            (shop["id"], RUCKSACK),
        )
    db.execute(
        "INSERT INTO postings(account_id, posting_number, status, shipment_date, local_state, "
        "label_saved_at, positions_count, items_count) VALUES(?, '49000000-7777-1', ?, ?, 'new', ?, 1, 1)",
        (mk["id"], ozon_store.STATUS_AWAITING_PACKAGING, "2099-01-01T00:00:00+00:00", db.now_iso()),
    )
    db.execute(
        "INSERT INTO posting_items(account_id, posting_number, sku, offer_id, name, quantity) "
        "VALUES(?, '49000000-7777-1', 'SKU-ANEX', 'ANEX-1', 'Эргорюкзак Anex Whiz-Hug Lownight', 1)",
        (mk["id"],),
    )
    return mk


def test_the_answer_comes_from_the_cabinet_that_has_the_order(client, cabinets, rucksack):
    """В шапке кабинет без заказа — ответ всё равно от того, где заказ есть.

    Так было на складе: в шапке «ANEX», скан рюкзака — «не нужен ни в одном
    отправлении», хотя в «МК» он лежит в «Ожидает сборки».
    """
    result = scan(client, RUCKSACK)
    assert "не нужен ни в одном" not in result["message"], result["message"]
    assert "Ожидает сборки" in result["message"], result["message"]
    assert result["shop"] == "МК"


def test_a_cabinet_filter_separates_two_shops_of_one_marketplace(client, cabinets, rucksack):
    """Два кабинета Ozon — фильтр по одному не пускает в другой.

    Ради этого фильтр и переделан с площадок на кабинеты: чип «Ozon» объединял
    оба магазина, а склад собирает их раздельно.
    """
    number = "49000000-7777-1"          # отправление кабинета «МК»

    other = scan(client, number, shop=cabinets["ozon"]["id"])
    assert "не найдено" in other["message"], other["message"]
    assert other.get("shop") != "МК"

    own = scan(client, number, shop=rucksack["id"])
    assert own["shop"] == "МК"
    assert "Ожидает сборки" in own["message"], own["message"]


def test_the_same_scan_gives_the_same_answer(client, cabinets, rucksack):
    """Один и тот же скан — один и тот же ответ: «текущего кабинета» нет вовсе."""
    first = scan(client, RUCKSACK)
    client.post("/api/pack/release")
    second = scan(client, RUCKSACK)
    assert (first["message"], first["shop"]) == (second["message"], second["shop"])


def test_an_unknown_code_answers_from_the_first_cabinet(client, cabinets):
    """Нераспознанный код отвечает первый кабинет под фильтром — по порядку «Настроек»."""
    result = scan(client, "НЕТ-ТАКОГО-КОДА")
    assert result.get("shop") == cabinets["ozon"]["title"]


def test_owner_agrees_with_the_scan_on_every_barcode(cabinets, user):
    """«Открою» от площадки — ровно тогда, когда её скан действительно открывает.

    Вся ошибка с рюкзаком была в расхождении: ядро спрашивало «чей товар» по
    одним правилам, а скан подбирал отправление по другим. Прогоняем весь
    каталог и сверяем ответ с делом.
    """
    from app.markets.ozon import pack as ozon_pack

    shop = cabinets["ozon"]
    codes = [row["barcode"] for row in db.query(
        "SELECT DISTINCT barcode FROM product_barcodes WHERE account_id = ? LIMIT 40", (shop["id"],))]
    assert codes, "каталог пуст — сверять нечего"

    def check() -> set:
        seen = set()
        for code in codes:
            answer = ozon_pack.owner(shop, user, code)
            result = ozon_pack.scan(shop, user, code)
            opened = result["status"] == "ok" and result["state"]["active"] is not None
            promised = bool(answer) and answer[0] == "product"
            assert promised == opened, (code, answer, result["message"])
            if opened:
                assert result["state"]["active"]["posting_number"] == answer[1], code
            ozon_pack.release(shop, user)
            seen.add(answer[0] if answer else None)
        return seen

    kinds = check()
    # Половину отправлений — назад в «Ожидает сборки», часть — в собранные: у
    # товаров появляются ответы «мой, но открыть нечего». Договор обязан
    # держаться при любом состоянии склада, а не только на свежих данных.
    numbers = [row["posting_number"] for row in db.query(
        "SELECT posting_number FROM postings WHERE account_id = ? ORDER BY posting_number", (shop["id"],))]
    for index, number in enumerate(numbers):
        if index % 2 == 0:
            db.execute("UPDATE postings SET status = ? WHERE account_id = ? AND posting_number = ?",
                       (ozon_store.STATUS_AWAITING_PACKAGING, shop["id"], number))
        elif index % 3 == 0:
            db.execute("UPDATE postings SET local_state = 'packed' WHERE account_id = ? AND posting_number = ?",
                       (shop["id"], number))
    kinds |= check()
    assert {"product", "known"} <= kinds, f"проверка вышла пустой: {kinds}"


# ------------------------------------------------------------------ остальное
def test_release_finds_the_open_cabinet(client, cabinets, user):
    """«Отменить сборку» ищет её там, где она открыта, а не в шапке."""
    scan(client, avito_label(cabinets))
    response = client.post("/api/pack/release")

    assert response.status_code == 200, response.text
    assert response.json()["state"]["active"] is None
    assert packing.started(user) is None


def test_completing_without_a_label_is_refused_where_there_is_no_such_thing(client, cabinets):
    """У Avito «завершить без скана» нет: заказ закрывает последняя единица товара."""
    scan(client, avito_label(cabinets))
    response = client.post("/api/pack/complete", json={})

    assert response.status_code == 400
    assert "последняя единица" in response.json()["detail"]


def test_completing_without_an_assembly_is_refused(client):
    response = client.post("/api/pack/complete", json={})
    assert response.status_code == 400
    assert "не начата" in response.json()["detail"]


def test_an_unknown_code_is_still_an_error(client):
    result = scan(client, "НЕТ-ТАКОГО-КОДА")
    assert result["status"] == "error"
    assert "не найден" in result["message"]


# ------------------------------------------------------------------ печать
def test_a_label_prints_from_another_cabinet(client, cabinets):
    """Наклейку открытого заказа печатаем, каким бы ни был кабинет в шапке.

    Раньше адрес печати был площадочным и требовал её кабинета: сборка Avito
    открывалась, а «Этикетка» отвечала отказом.
    """
    order = db.query_one(
        "SELECT id FROM avito_orders WHERE account_id = ? AND status = 'ready_to_ship' LIMIT 1",
        (cabinets["avito"]["id"],),
    )["id"]
    response = client.get(f"/api/pack/label/avito/{order}.pdf")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.content[:4] == b"%PDF"


def test_every_market_prints_the_same_way(client, cabinets):
    """Адрес печати один на все площадки — различается только код в нём."""
    ozon = db.query_one(
        "SELECT posting_number FROM postings WHERE account_id = ? AND status = ? LIMIT 1",
        (cabinets["ozon"]["id"], ozon_store.STATUS_AWAITING_DELIVER),
    )["posting_number"]
    yandex = db.query_one(
        "SELECT id FROM yandex_orders WHERE account_id = ? LIMIT 1", (cabinets["yandex"]["id"],)
    )["id"]

    for code, number in (("ozon", ozon), ("yandex", yandex)):
        response = client.get(f"/api/pack/label/{code}/{number}.pdf")
        assert response.status_code == 200, (code, response.text)
        assert response.content[:4] == b"%PDF", code


def test_an_unknown_order_has_no_label(client):
    assert client.get("/api/pack/label/ozon/НЕТ-ТАКОГО.pdf").status_code == 404


# ------------------------------------------------------------------ страница
def test_the_page_carries_every_market_card(client):
    """Карточки всех площадок подключены: открытый заказ может быть из любой."""
    page = client.get("/pack").text
    for code in ("ozon", "avito", "yandex"):
        assert f"/static/{code}/pack.js" in page, code


def test_the_tiles_follow_the_filter(client, cabinets):
    """«Все заказы» — общие плитки, выбран кабинет — плитки его площадки."""
    everything = client.get("/pack").text
    assert "В работе" in everything and "Горит сегодня" in everything

    only_ozon = client.get(f"/pack?shop={cabinets['ozon']['id']}").text
    assert "Ожидает отгрузки" in only_ozon
    assert "Возвраты к выдаче" in only_ozon, "плитки должны быть озоновские"
