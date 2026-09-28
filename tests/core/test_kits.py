"""Комплекты и артикул основной карточки.

Комплект — сам товар и то, что в него вкладывают: термокружка и чехол к ней.
Сборщик сканирует основной товар, и панель сразу просит вложение: пока его не
отсканировали, товар не засчитан, а любой другой скан — ошибка. Вложение раньше
основного — тоже ошибка. Правило общее для всех площадок: его держит ядро
(core/packing.py) до того, как скан уйдёт площадке.

Сопоставленные карточки — одна коробка: штрихкод годится любой из группы, даже
из другого кабинета, а артикул сборщик видит один — основной карточки. У
объявления Avito своих артикула и ШК может не быть: они берутся у группы.
"""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import catalog, db, linked, product_links, product_sets
from app.core import packing as core_packing
from app.main import app
from app.markets.avito import pack as avito_pack
from app.markets.avito import store as avito_store
from app.markets.avito import sync as avito_sync
from app.markets.ozon import pack as ozon_pack
from app.markets.yandex import client as yandex
from app.markets.yandex import pack as yandex_pack
from app.markets.yandex import store as yandex_store
from app.markets.yandex import sync as yandex_sync
from app.routes.products import catalog_rows
from tests.conftest import barcode_of, pick_posting

STATIC = Path(__file__).resolve().parents[2] / "app"
INSERT = "9990000000511"        # вложение без карточки — только штрихкодом
FOREIGN = "4650000000011"       # штрихкод карточки другого кабинета


def put_card(account, sku, name, *, barcodes=(), offer_id=None):
    with db.write() as conn:
        catalog.save(conn, account["id"], [{"sku": sku, "offer_id": offer_id or sku, "name": name,
                                             "barcodes": list(barcodes)}])
    return account["id"], str(sku)


def link(main, *others):
    product_links.link(main, list(others), user={"login": "admin"})


def make_kit(account_id, sku, parts=None):
    return product_sets.save(account_id, sku, parts or [{"barcode": INSERT, "title": "Чехол"}],
                             user={"login": "admin"}, kind="kit")


def scan(user, code):
    """Скан через общее рабочее место — там и живёт правило комплекта."""
    return core_packing.scan(core_packing.shops(), user, code)


def item_of(state, sku):
    return next(item for item in state["items"] if item["sku"] == sku)


def stranger_code(account, exclude):
    marks = ",".join("?" for _ in exclude)
    row = db.query_one(f"SELECT barcode FROM product_barcodes WHERE account_id = ? AND sku NOT IN ({marks}) LIMIT 1",
                       [account["id"]] + list(exclude))
    return row["barcode"]


@pytest.fixture
def kit(account, sample_data, user):
    """Отправление Ozon в одну позицию, объявленную комплектом, — взято в работу."""
    posting = pick_posting(positions=1)
    sku = posting["items"][0]["sku"]
    make_kit(account["id"], sku)
    ozon_pack.select_posting(account, user, posting["posting_number"])
    return {"posting": posting, "sku": sku, "main": barcode_of(sku), "name": posting["items"][0]["name"]}


# ------------------------------------------------------------------ состав
def test_kit_is_stored_with_its_kind(account, sample_data):
    sku = pick_posting(positions=1)["items"][0]["sku"]
    saved = make_kit(account["id"], sku)
    assert saved["kind"] == "kit"
    assert product_sets.kinds()[(account["id"], sku)] == "kit"
    # Набор по-прежнему набор: вид по умолчанию.
    other = pick_posting(positions=2)["items"][0]["sku"]
    assert product_sets.save(account["id"], other, [{"barcode": INSERT}])["kind"] == "set"


def test_kit_refusals_speak_about_inserts(account, sample_data):
    sku = pick_posting(positions=1)["items"][0]["sku"]
    with pytest.raises(product_sets.SetError, match="хотя бы одно вложение"):
        product_sets.save(account["id"], sku, [], kind="kit")
    with pytest.raises(product_sets.SetError, match="сам в себя"):
        product_sets.save(account["id"], sku, [{"sku": sku}], kind="kit")
    with pytest.raises(product_sets.SetError, match="Не выбран основной товар"):
        product_sets.save(account["id"], "", [{"barcode": INSERT}], kind="kit")


def test_kit_progress_waits_for_the_insert_after_the_main():
    parts = [{"part_key": "bc:1", "barcode": "1", "title": "Чехол", "quantity": 1, "kind": "kit"}]
    got, extra = product_sets.progress("sku", 2, 0, parts, {})
    assert got == 0 and extra["kind"] == "kit" and extra["waiting"] is None
    main, insert = extra["parts"]
    assert main["main"] is True and main["need"] == 2 and insert["need"] == 2

    got, extra = product_sets.progress("sku", 2, 1, parts, {})
    assert got == 0, "без вложения основной товар позицию не закрывает"
    assert extra["waiting"] == "Чехол" and extra["parts"][1]["next"] is True

    got, extra = product_sets.progress("sku", 2, 1, parts, {"sku#bc:1": 1})
    assert got == 1 and extra["waiting"] is None, "вторая кружка ещё не отсканирована — вложение не ждут"


# ------------------------------------------------------------------ сборка Ozon
def test_insert_is_asked_right_after_the_main(account, kit, user):
    opened = scan(user, kit["main"])
    assert opened["status"] == "ok", opened["message"]
    item = item_of(opened["state"], kit["sku"])
    assert item["kind"] == "kit" and item["ok"] is False
    assert item["waiting"] == "Чехол"
    assert [part["scanned"] for part in item["parts"]] == [1, 0]

    # Любой другой скан — ошибка, и ничего не засчитывается.
    for code in (stranger_code(account, [kit["sku"]]), kit["main"], kit["posting"]["posting_number"]):
        wrong = scan(user, code)
        assert wrong["status"] == "error" and wrong["action"] == "kit_order", code
        assert wrong["message"] == "Сначала вложите «Чехол» и отсканируйте его"
        assert item_of(wrong["state"], kit["sku"])["parts"][0]["scanned"] == 1

    done = scan(user, INSERT)
    assert done["status"] == "ok", done["message"]
    item = item_of(done["state"], kit["sku"])
    assert item["ok"] is True and item["waiting"] is None
    assert done["state"]["complete"] is True

    closed = scan(user, kit["posting"]["posting_number"])
    assert closed["action"] == "completed", closed["message"]


def test_insert_before_the_main_is_refused(account, kit, user):
    result = scan(user, INSERT)
    assert result["status"] == "error" and result["action"] == "kit_order"
    assert result["message"] == f"Сначала отсканируйте «{kit['name']}», потом вложите «Чехол»"
    assert item_of(result["state"], kit["sku"])["parts"][1]["scanned"] == 0


def test_two_kits_are_main_insert_main_insert(account, kit, user):
    db.execute("UPDATE posting_items SET quantity = 2 WHERE account_id = ? AND posting_number = ?",
               (account["id"], kit["posting"]["posting_number"]))
    assert scan(user, kit["main"])["status"] == "ok"
    first = scan(user, INSERT)
    assert item_of(first["state"], kit["sku"])["scanned"] == 1
    assert scan(user, INSERT)["action"] == "kit_order", "второе вложение — только после второй кружки"
    assert scan(user, kit["main"])["status"] == "ok"
    second = scan(user, INSERT)
    assert second["state"]["complete"] is True, second["message"]


def test_linked_barcodes_of_other_cabinets_work_for_main_and_insert(account, sample_data, yandex_account, user):
    """Сопоставленная карточка — та же коробка: годится её штрихкод, даже из другого кабинета."""
    posting = pick_posting(positions=1)
    sku = posting["items"][0]["sku"]
    case = put_card(account, "OZ-CASE", "Чехол", barcodes=["4600000000901"])
    link(case, put_card(yandex_account, "YA-CASE", "Чехол Маркета", barcodes=["4650000000902"]))
    link((account["id"], sku), put_card(yandex_account, "YA-MUG", "Кружка Маркета", barcodes=[FOREIGN]))
    make_kit(account["id"], sku, [{"sku": "OZ-CASE"}])
    ozon_pack.select_posting(account, user, posting["posting_number"])

    assert scan(user, FOREIGN)["status"] == "ok", "основной товар — по ШК карточки Маркета"
    waiting = scan(user, "0000000000000")
    assert waiting["action"] == "kit_order"
    done = scan(user, "4650000000902")
    assert done["status"] == "ok", done["message"]
    assert done["state"]["complete"] is True


def test_insert_alone_does_not_open_a_posting(account, kit, user):
    ozon_pack.release(account, user)
    result = ozon_pack.scan(account, user, INSERT)
    assert result["state"]["active"] is None, "комплект начинают с основного товара"


def test_label_scan_names_the_missing_insert(account, kit, user, monkeypatch):
    scan(user, kit["main"])
    scan(user, INSERT)
    ozon_pack.release(account, user)
    ozon_pack.select_posting(account, user, kit["posting"]["posting_number"])
    result = ozon_pack.scan(account, user, kit["posting"]["posting_number"])
    assert result["action"] == "incomplete"
    assert "(комплект)" in result["message"] and "Чехол — 1 шт" in result["message"]


# ------------------------------------------------------------------ Маркет и Avito
@pytest.fixture
def market_order(yandex_account, sample_data, account):
    fake = yandex.get_client(yandex_account)
    for index in (1, 2):
        order = fake._orders[str(80000000 + int(fake.business_id) % 1000 * 100000 + index)]
        order["substatus"] = yandex.SUBSTATUS_READY_TO_SHIP
    yandex_sync.sync_yandex(yandex_account)
    order = db.query_one("SELECT * FROM yandex_orders WHERE account_id = ? AND substatus = ? ORDER BY id LIMIT 1",
                         (yandex_account["id"], yandex.SUBSTATUS_READY_TO_SHIP))
    db.execute("DELETE FROM yandex_order_items WHERE account_id = ? AND order_id = ? AND item_id NOT IN "
               "(SELECT item_id FROM yandex_order_items WHERE account_id = ? AND order_id = ? LIMIT 1)",
               (yandex_account["id"], order["id"], yandex_account["id"], order["id"]))
    db.execute("UPDATE yandex_order_items SET offer_id = 'YA-OWN', quantity = 1 WHERE account_id = ? AND order_id = ?",
               (yandex_account["id"], order["id"]))
    card = put_card(yandex_account, "YA-OWN", "Кружка Маркета")
    main = put_card(account, "OZ-LINK", "Кружка", barcodes=[FOREIGN], offer_id="OZ-ART")
    link(main, card)
    return {"order_id": order["id"], "card": card, "main": main}


def test_market_item_shows_the_main_article(yandex_account, market_order):
    item = yandex_store.yandex_items(yandex_account["id"], market_order["order_id"])[0]
    assert item["offer_id"] == "YA-OWN"
    assert item["article"] == "OZ-ART", "артикул — основной карточки"
    assert item["barcodes"] == [FOREIGN], "ШК — любой из группы"


def test_market_kit_follows_the_same_order(yandex_account, market_order, user):
    make_kit(market_order["main"][0], "OZ-LINK")
    yandex_pack.select_order(yandex_account, user, market_order["order_id"])
    assert scan(user, INSERT)["action"] == "kit_order"
    assert scan(user, FOREIGN)["status"] == "ok"
    assert scan(user, FOREIGN)["message"] == "Сначала вложите «Чехол» и отсканируйте его"
    done = scan(user, INSERT)
    assert done["status"] == "ok", done["message"]
    assert done["state"]["complete"] is True


@pytest.fixture
def avito_order(avito_account, account, sample_data):
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
    main = put_card(account, "OZ-AV", item["title"], barcodes=[FOREIGN], offer_id="OZ-AV-ART")
    link(main, card)
    return {"order": dict(order), "main": main}


def test_avito_ad_takes_article_and_barcodes_of_the_group(avito_account, avito_order, user):
    opened = avito_pack.scan(avito_account, user, avito_order["order"]["marketplace_id"] or avito_order["order"]["id"])
    unit = opened["state"]["items"][0]
    assert unit["article"] == "OZ-AV-ART"
    assert unit["barcodes"] == [FOREIGN]


def test_avito_kit_follows_the_same_order(avito_account, avito_order, user):
    make_kit(avito_order["main"][0], "OZ-AV")
    opened = avito_pack.scan(avito_account, user, avito_order["order"]["marketplace_id"] or avito_order["order"]["id"])
    assert opened["action"] == "order_opened", opened["message"]
    for _ in range(opened["state"]["total"]):
        assert scan(user, FOREIGN)["status"] == "ok"
        assert scan(user, "0000000000000")["action"] == "kit_order"
        result = scan(user, INSERT)
        assert result["status"] == "ok", result["message"]
    assert result["action"] == "completed"


# ------------------------------------------------------------------ артикул
def test_article_is_the_main_cards(account, sample_data, yandex_account, user):
    posting = pick_posting(positions=1)
    sku = posting["items"][0]["sku"]
    own = posting["items"][0]["offer_id"]
    assert linked.article(account["id"], sku, own) == own, "без сопоставления — свой"

    main = put_card(yandex_account, "YA-ART", "Кружка", offer_id="YA-ART")
    link(main, (account["id"], sku))
    assert linked.article(account["id"], sku, own) == "YA-ART"
    ozon_pack.select_posting(account, user, posting["posting_number"])
    assert item_of(ozon_pack.load_state(account, user), sku)["article"] == "YA-ART"


def test_article_falls_back_when_the_main_has_none(account, sample_data, avito_account):
    sku = pick_posting(positions=1)["items"][0]["sku"]
    main = put_card(avito_account, "777", "Объявление")
    db.execute("UPDATE products SET offer_id = NULL WHERE account_id = ? AND sku = '777'", (avito_account["id"],))
    link(main, (account["id"], sku))
    assert linked.article(account["id"], sku, "OWN") == "OWN"
    assert linked.article(avito_account["id"], "777", None) is not None, "у объявления — артикул группы"


# ------------------------------------------------------------------ «Товары»
def test_catalog_row_takes_barcode_of_any_card_when_the_main_has_none(account, yandex_account, sample_data):
    main = put_card(yandex_account, "YA-MAIN", "Кружка", offer_id="YA-MAIN")
    put_card(account, "OZ-BC", "Кружка", barcodes=["4600000000999"], offer_id="OZ-BC")
    link(main, (account["id"], "OZ-BC"))
    row = next(r for r in catalog_rows(q="Кружка") if r.get("group_id"))
    assert row["offer_id"] == "YA-MAIN", "артикул — основной"
    assert row["barcodes"] == ["4600000000999"]
    assert row["barcode_from"]["shop"] == account["title"]

    with TestClient(app, follow_redirects=False) as client:
        client.post("/login", data={"login": "admin", "password": "test-admin-pass"})
        page = client.get("/products", params={"q": "Кружка"}).text
    assert "у основной нет — с карточки" in page


@pytest.fixture
def client(sample_data):
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.post("/login", data={"login": "admin", "password": "test-admin-pass"})
        page = test_client.get("/products?tab=sets")
        test_client.headers["X-CSRF-Token"] = re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1)
        yield test_client


def test_kit_is_saved_and_deleted_through_the_api(client, account):
    sku = pick_posting(positions=1)["items"][0]["sku"]
    saved = client.post("/api/products/sets", json={"account_id": account["id"], "sku": sku, "kind": "kit",
                                                    "parts": [{"barcode": INSERT, "title": "Чехол"}]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["message"] == "Комплект сохранён: вложений 1"
    assert client.get(f"/api/products/{account['id']}/{sku}").json()["set"]["kind"] == "kit"

    page = client.get("/products?tab=sets").text
    assert "Наборы и комплекты" in page and 'id="kit-new"' in page
    assert '<span class="badge kit">комплект</span>' in page
    assert "основной — сканируют первым" in page and "вложить сразу после основного" in page
    catalog_page = client.get("/products").text
    assert "Набор / комплект" in catalog_page and "Сделать набором" not in catalog_page

    removed = client.delete(f"/api/products/sets/{account['id']}/{sku}")
    assert removed.json()["message"] == "Комплект удалён: товар снова обычный"


# ------------------------------------------------------------------ окно сборки
def test_window_draws_the_kit_rows_without_role_words():
    script = (STATIC / "static" / "market_pack.js").read_text(encoding="utf-8")
    parts = script[script.index("function packPartRow"):script.index("function packUrgency")]
    assert "основной" not in parts.lower() and "вложить" not in parts.lower()
    assert "article" in parts, "в строке — название, артикул и ШК"
    assert "item.wait" in script and "Вложите «" in script


@pytest.mark.parametrize("market", ["ozon", "yandex", "avito"])
def test_every_market_shows_the_main_article_and_the_wait(market):
    script = (STATIC / "markets" / market / "static" / "pack.js").read_text(encoding="utf-8")
    assert "item.article" in script
    assert "wait: item.waiting" in script
