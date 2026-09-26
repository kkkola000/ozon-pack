"""Сопоставленные карточки на сборке: одна коробка — любой её штрихкод.

Сопоставление говорит: эти карточки разных кабинетов — один товар на полке.
Отсюда три правила, которые здесь и проверяются:

* штрихкод любой карточки группы подходит к заказу любой площадки;
* фото берётся у сопоставленной карточки, если своего нет;
* набор, заданный одной карточке, собирается по частям во всей группе, а
  часть узнаётся и по штрихкоду сопоставленной с ней карточки.

У Avito своих штрихкодов нет: сопоставленное объявление сверяется со
штрихкодами группы, несопоставленное — как раньше, без сверки.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app.core import catalog, db, linked, pack_state, product_links, product_sets
from app.core import packing as core_packing
from app.main import app
from app.markets.avito import pack as avito_pack
from app.markets.avito import store as avito_store
from app.markets.avito import sync as avito_sync
from app.markets.ozon import pack as ozon_pack
from app.markets.ozon import store as ozon_store
from app.markets.yandex import client as yandex
from app.markets.yandex import pack as yandex_pack
from app.markets.yandex import store as yandex_store
from app.markets.yandex import sync as yandex_sync
from app.routes.products import catalog_rows
from tests.conftest import barcode_of, pick_posting

FOREIGN = "4650000000011"      # штрихкод карточки Маркета, в Ozon его нет
PART_A = "9990000000123"       # части набора — только штрихкодом, без товара
PART_B = "9990000000130"


def put_card(account, sku, name, *, barcodes=(), image=None, offer_id=None):
    """Карточка в каталоге кабинета — как её сохраняет обход каталога."""
    with db.write() as conn:
        catalog.save(conn, account["id"], [{"sku": sku, "offer_id": offer_id or sku, "name": name,
                                             "image": image, "barcodes": list(barcodes)}])
    return account["id"], str(sku)


def link(main, *others):
    product_links.link(main, list(others), user={"login": "admin"})


def item_of(state, sku):
    return next(item for item in state["items"] if item["sku"] == sku)


@pytest.fixture
def ozon_item(account, sample_data):
    """Отправление Ozon в одну позицию и карточка его товара в каталоге."""
    posting = pick_posting(positions=1)
    sku = posting["items"][0]["sku"]
    return {"posting": posting, "sku": sku, "card": (account["id"], sku)}


# ------------------------------------------------------------------ Ozon: штрихкод
def test_barcode_of_linked_card_opens_ozon_posting(account, ozon_item, yandex_account, user):
    market_card = put_card(yandex_account, "YA-1", "Кружка", barcodes=[FOREIGN])

    before = ozon_pack.scan(account, user, FOREIGN)
    assert before["status"] == "error", "несопоставленный штрихкод чужой площадки не должен подходить"
    assert before["state"]["active"] is None

    link(ozon_item["card"], market_card)
    # Скан идёт через общее рабочее место: кабинет находит ядро.
    result = core_packing.scan(core_packing.shops(), user, FOREIGN)
    assert result["action"] == "posting_selected", result["message"]
    assert result["market"] == "ozon"
    assert item_of(result["state"], ozon_item["sku"])["scanned"] == 1


def test_barcode_of_linked_card_counts_in_open_posting(account, ozon_item, yandex_account, user):
    link(ozon_item["card"], put_card(yandex_account, "YA-1", "Кружка", barcodes=[FOREIGN]))
    ozon_pack.select_posting(account, user, ozon_item["posting"]["posting_number"])

    result = ozon_pack.scan(account, user, FOREIGN)
    assert result["status"] == "ok", result["message"]
    assert item_of(result["state"], ozon_item["sku"])["scanned"] == 1


def test_foreign_barcode_without_link_is_not_counted(account, ozon_item, yandex_account, user):
    put_card(yandex_account, "YA-1", "Кружка", barcodes=[FOREIGN])
    ozon_pack.select_posting(account, user, ozon_item["posting"]["posting_number"])

    result = ozon_pack.scan(account, user, FOREIGN)
    assert result["status"] == "error"
    assert item_of(result["state"], ozon_item["sku"])["scanned"] == 0


def test_posting_takes_photo_and_barcodes_from_linked_card(account, ozon_item, yandex_account):
    db.execute("UPDATE products SET image = NULL WHERE account_id = ? AND sku = ?", ozon_item["card"])
    link(ozon_item["card"], put_card(yandex_account, "YA-1", "Кружка", barcodes=[FOREIGN],
                                     image="https://img.example/ya-1.jpg"))

    items = ozon_store.posting_items(account["id"], ozon_item["posting"]["posting_number"])
    item = next(i for i in items if i["sku"] == ozon_item["sku"])
    assert item["image"] == "https://img.example/ya-1.jpg"
    assert item["barcodes"][0] == barcode_of(ozon_item["sku"]), "свой штрихкод — первым"
    assert FOREIGN in item["barcodes"]


def test_own_photo_is_stronger_than_linked(account, ozon_item, yandex_account):
    own = "https://img.example/own.jpg"
    db.execute("UPDATE products SET image = ? WHERE account_id = ? AND sku = ?", (own, *ozon_item["card"]))
    link(ozon_item["card"], put_card(yandex_account, "YA-1", "Кружка", image="https://img.example/other.jpg"))
    assert linked.extras(*ozon_item["card"], image=own)[1] == own
    items = ozon_store.posting_items(account["id"], ozon_item["posting"]["posting_number"])
    assert next(i for i in items if i["sku"] == ozon_item["sku"])["image"] == own


# ------------------------------------------------------------------ Ozon: наборы
def test_set_of_linked_card_is_packed_by_parts_in_ozon(account, ozon_item, yandex_account, user):
    market_card = put_card(yandex_account, "YA-S", "Набор кружек")
    product_sets.save(yandex_account["id"], "YA-S", [{"barcode": PART_A, "title": "Кружка"},
                                                     {"barcode": PART_B, "title": "Коробка"}])
    link(ozon_item["card"], market_card)
    assert [card["sku"] for card in product_sets.shared_with(*market_card)] == [ozon_item["sku"]]

    opened = ozon_pack.select_posting(account, user, ozon_item["posting"]["posting_number"])
    item = item_of(opened["state"], ozon_item["sku"])
    assert item["is_set"] is True
    assert [part["name"] for part in item["parts"]] == ["Кружка", "Коробка"]

    first = ozon_pack.scan(account, user, PART_A)
    assert first["action"] == "set_part_scanned", first["message"]
    last = ozon_pack.scan(account, user, PART_B)
    assert last["action"] == "ready_for_label", last["message"]
    assert item_of(last["state"], ozon_item["sku"])["ok"] is True


def test_part_is_recognised_by_barcode_of_linked_card(account, ozon_item, yandex_account, user):
    part = db.query_one(
        "SELECT sku FROM products WHERE account_id = ? AND sku != ? "
        "AND sku IN (SELECT sku FROM product_barcodes WHERE account_id = ?) LIMIT 1",
        (account["id"], ozon_item["sku"], account["id"]),
    )["sku"]
    product_sets.save(account["id"], ozon_item["sku"], [{"sku": part, "quantity": 1}])
    link((account["id"], part), put_card(yandex_account, "YA-P", "Часть", barcodes=[FOREIGN]))
    ozon_pack.select_posting(account, user, ozon_item["posting"]["posting_number"])

    result = ozon_pack.scan(account, user, FOREIGN)
    assert result["status"] == "ok", result["message"]
    assert item_of(result["state"], ozon_item["sku"])["ok"] is True


def test_own_set_wins_over_the_group_set(account, ozon_item, yandex_account):
    market_card = put_card(yandex_account, "YA-S", "Набор кружек")
    product_sets.save(yandex_account["id"], "YA-S", [{"barcode": PART_A}])
    product_sets.save(account["id"], ozon_item["sku"], [{"barcode": PART_B}])
    link(ozon_item["card"], market_card)

    assert product_sets.source_of(*ozon_item["card"]) == ozon_item["card"]
    assert product_sets.shared_with(*market_card) == [], "у карточки Ozon свой состав"


def test_set_does_not_spread_without_link(account, ozon_item, yandex_account):
    put_card(yandex_account, "YA-S", "Набор кружек")
    product_sets.save(yandex_account["id"], "YA-S", [{"barcode": PART_A}])
    assert product_sets.source_of(*ozon_item["card"]) is None
    assert product_sets.parents_of(account["id"], barcodes=[PART_A]) == []


# ------------------------------------------------------------------ Маркет
@pytest.fixture
def market_item(yandex_account, sample_data, account):
    """Позиция заказа Маркета «Ожидает отгрузки» с артикулом, которого нет в Ozon.

    Штрихкод по артикулу её не найдёт — только сопоставление.
    """
    fake = yandex.get_client(yandex_account)
    for index in (1, 2):
        order = fake._orders[str(80000000 + int(fake.business_id) % 1000 * 100000 + index)]
        order["substatus"] = yandex.SUBSTATUS_READY_TO_SHIP
    yandex_sync.sync_yandex(yandex_account)
    order = db.query_one(
        "SELECT * FROM yandex_orders WHERE account_id = ? AND substatus = ? ORDER BY id LIMIT 1",
        (yandex_account["id"], yandex.SUBSTATUS_READY_TO_SHIP),
    )
    item = db.query_one("SELECT * FROM yandex_order_items WHERE account_id = ? AND order_id = ? LIMIT 1",
                        (yandex_account["id"], order["id"]))
    db.execute("UPDATE yandex_order_items SET offer_id = 'YA-OWN' WHERE account_id = ? AND order_id = ? "
               "AND item_id = ?", (yandex_account["id"], order["id"], item["item_id"]))
    card = put_card(yandex_account, "YA-OWN", "Кружка Маркета")
    ozon_card = put_card(account, "OZ-LINK", "Кружка", barcodes=[FOREIGN], offer_id="OZ-ART",
                         image="https://img.example/oz.jpg")
    return {"order_id": order["id"], "item_id": item["item_id"], "card": card, "ozon_card": ozon_card}


def test_linked_barcode_opens_market_order_with_other_article(yandex_account, market_item, user):
    before = yandex_pack.scan(yandex_account, user, FOREIGN)
    assert before["action"] != "order_selected", "без сопоставления артикулы разные — заказ не найти"
    assert before["state"]["active"] is None

    link(market_item["ozon_card"], market_item["card"])
    items = yandex_store.yandex_items(yandex_account["id"], market_item["order_id"])
    item = next(i for i in items if i["item_id"] == market_item["item_id"])
    assert item["barcodes"] == [FOREIGN]
    assert item["image"] == "https://img.example/oz.jpg"

    result = yandex_pack.scan(yandex_account, user, FOREIGN)
    assert result["action"] == "order_selected", result["message"]
    assert result["state"]["active"]["id"] == market_item["order_id"]
    row = next(i for i in result["state"]["items"] if i["item_id"] == market_item["item_id"])
    assert row["scanned"] == 1


def test_market_set_of_linked_card_is_packed_by_parts(yandex_account, market_item, user):
    product_sets.save(market_item["ozon_card"][0], "OZ-LINK", [{"barcode": PART_A, "title": "Кружка"},
                                                               {"barcode": PART_B, "title": "Коробка"}])
    link(market_item["ozon_card"], market_item["card"])

    # Место свободно: часть набора сама находит заказ и засчитывается.
    opened = yandex_pack.scan(yandex_account, user, PART_A)
    assert opened["action"] == "order_selected", opened["message"]
    row = next(i for i in opened["state"]["items"] if i["item_id"] == market_item["item_id"])
    assert row["is_set"] is True
    assert [part["scanned"] for part in row["parts"]] == [1, 0]
    assert any("Коробка" in line for line in yandex_pack.missing_items(opened["state"]))

    done = yandex_pack.scan(yandex_account, user, PART_B)
    assert done["status"] == "ok", done["message"]
    row = next(i for i in done["state"]["items"] if i["item_id"] == market_item["item_id"])
    assert row["ok"] is True
    again = yandex_pack.scan(yandex_account, user, PART_B)
    assert again["action"] == "extra_product"


# ------------------------------------------------------------------ Avito
@pytest.fixture
def avito_unit(avito_account, account, sample_data):
    """Заказ Avito в одну позицию, объявление которого есть в каталоге панели."""
    avito_sync.sync_avito(avito_account)
    for order in db.query("SELECT * FROM avito_orders WHERE account_id = ? AND status = 'ready_to_ship' "
                          "ORDER BY id", (avito_account["id"],)):
        items = avito_store.avito_items(avito_account["id"], order["id"])
        if len(items) == 1:
            break
    else:
        pytest.fail("в подделке Avito нет заказа в одну позицию")
    item = items[0]
    card = put_card(avito_account, str(item["avito_id"]), item["title"])
    ozon_card = put_card(account, "OZ-AV", item["title"], barcodes=[FOREIGN], image="https://img.example/av.jpg")
    return {"order": dict(order), "item": item, "card": card, "ozon_card": ozon_card}


def open_avito(avito_account, user, order):
    opened = avito_pack.scan(avito_account, user, order["marketplace_id"] or order["id"])
    assert opened["action"] == "order_opened", opened["message"]
    return opened["state"]


def test_avito_linked_ad_accepts_only_its_barcodes(avito_account, avito_unit, user, account):
    link(avito_unit["ozon_card"], avito_unit["card"])
    state = open_avito(avito_account, user, avito_unit["order"])
    assert all(unit["checked"] for unit in state["items"])
    # Своё фото из заказа сильнее, нет его — фото сопоставленной карточки.
    assert state["items"][0]["image"] == (avito_unit["item"].get("image") or "https://img.example/av.jpg")

    other = db.query_one("SELECT barcode FROM product_barcodes WHERE account_id = ? AND sku != 'OZ-AV' LIMIT 1",
                         (account["id"],))["barcode"]
    wrong = avito_pack.scan(avito_account, user, other)
    assert wrong["action"] == "wrong_product", wrong["message"]
    assert wrong["message"].startswith("СТОП")
    assert wrong["state"]["done"] == 0

    for _ in range(state["total"]):
        result = avito_pack.scan(avito_account, user, FOREIGN)
        assert result["status"] == "ok", result["message"]
    assert result["action"] == "completed"


def test_avito_unlinked_ad_still_takes_any_code(avito_account, avito_unit, user):
    state = open_avito(avito_account, user, avito_unit["order"])
    assert not any(unit["checked"] for unit in state["items"])
    result = avito_pack.scan(avito_account, user, "ЛЮБОЙ-КОД-1")
    assert result["status"] == "ok", result["message"]


def test_avito_set_is_collected_by_parts(avito_account, avito_unit, user):
    product_sets.save(avito_unit["ozon_card"][0], "OZ-AV", [{"barcode": PART_A, "title": "Кружка"},
                                                            {"barcode": PART_B, "title": "Коробка"}])
    link(avito_unit["ozon_card"], avito_unit["card"])
    state = open_avito(avito_account, user, avito_unit["order"])
    assert state["items"][0]["is_set"] is True

    wrong = avito_pack.scan(avito_account, user, "ЧУЖОЙ-КОД")
    assert wrong["action"] == "wrong_product", "набор сверяется по частям"

    for _ in range(state["total"]):
        first = avito_pack.scan(avito_account, user, PART_A)
        assert first["action"] == "set_part_scanned", first["message"]
        result = avito_pack.scan(avito_account, user, PART_B)
        assert result["status"] == "ok", result["message"]
    assert result["action"] == "completed"


def test_avito_state_from_before_the_update_is_read(avito_account, avito_unit, user):
    """Сборка, начатая до обновления, хранила просто список штрихкодов."""
    open_avito(avito_account, user, avito_unit["order"])
    with db.write() as conn:
        pack_state.save(conn, avito_account, user, avito_unit["order"]["id"], ["СТАРЫЙ-КОД"])
    state = avito_pack.load_state(avito_account, user)
    assert state["items"][0]["scanned"] is True
    assert state["items"][0]["barcode"] == "СТАРЫЙ-КОД"
    assert state["done"] == 1


def test_avito_board_shows_photo_of_linked_card(avito_account, avito_unit):
    db.execute("UPDATE avito_order_items SET image = NULL WHERE account_id = ?", (avito_account["id"],))
    link(avito_unit["ozon_card"], avito_unit["card"])
    row = db.query_one("SELECT * FROM avito_orders WHERE account_id = ? AND id = ?",
                       (avito_account["id"], avito_unit["order"]["id"]))
    assert avito_store.board_card(row)["image"] == "https://img.example/av.jpg"


# ------------------------------------------------------------------ «Товары»
def test_catalog_row_of_group_takes_any_photo(account, yandex_account, sample_data):
    main = put_card(yandex_account, "YA-MAIN", "Кружка", barcodes=[FOREIGN])
    other = put_card(account, "OZ-PHOTO", "Кружка", image="https://img.example/oz-photo.jpg")
    link(main, other)
    row = next(r for r in catalog_rows(q="Кружка") if r.get("group_id"))
    assert (row["account_id"], row["sku"]) == main, "основная карточка — Маркета"
    assert row["image"] == "https://img.example/oz-photo.jpg"


def test_products_page_marks_the_set_of_the_whole_group(account, yandex_account, sample_data):
    main = put_card(yandex_account, "YA-MAIN", "Кружка", barcodes=[FOREIGN])
    other = put_card(account, "OZ-PHOTO", "Кружка")
    link(main, other)
    product_sets.save(account["id"], "OZ-PHOTO", [{"barcode": PART_A}])

    with TestClient(app, follow_redirects=False) as client:
        client.post("/login", data={"login": "admin", "password": "test-admin-pass"})
        page = client.get("/products", params={"q": "Кружка"}).text
        assert re.search(rf'edit={account["id"]}:OZ-PHOTO">\s*Состав', page), "«Состав» ведёт к карточке набора"
        sets_page = client.get("/products", params={"tab": "sets"}).text
        assert "Действует и для сопоставленных" in sets_page
