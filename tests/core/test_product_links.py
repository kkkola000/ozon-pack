"""Сопоставление карточек: один товар склада — несколько карточек площадок.

Один и тот же товар лежит в кабинетах под разными карточками. Для площадки это
разные товары, для склада — одна коробка. Сопоставление сводит их в один товар,
и главное требование к нему такое: панель ничего не связывает молча, а всё
связанное можно развязать обратно.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app.core import accounts, catalog, db, product_links, sync
from app.main import app
from app.markets.yandex import sync as yandex_sync


@pytest.fixture
def two_shops(sample_data):
    """Два кабинета с каталогами: Ozon и Маркет, артикулы у них одинаковые."""
    ozon = accounts.default_account()
    catalog.refresh(ozon)
    market = accounts.get(accounts.create("yandex", "Маркет", "9000001", "test-token"))
    yandex_sync.sync_yandex(market)
    catalog.refresh(market)
    return {"ozon": ozon, "market": market}


@pytest.fixture
def client(two_shops):
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.post("/login", data={"login": "admin", "password": "test-admin-pass"})
        page = test_client.get("/products")
        test_client.headers["X-CSRF-Token"] = re.search(
            r'name="csrf-token" content="([^"]*)"', page.text
        ).group(1)
        yield test_client


def article_of(shop, sku):
    row = db.query_one("SELECT offer_id FROM products WHERE account_id = ? AND sku = ?",
                       (shop["id"], sku))
    return row["offer_id"] if row else None


def any_shared_article(two_shops) -> str:
    """Артикул, который есть в обоих кабинетах, — их каталоги совпадают."""
    found = product_links.suggestions()
    assert found, "в подделках нет одинаковых артикулов — проверять нечего"
    return found[0]["article"]


# ------------------------------------------------------------------ предложения
def test_same_article_in_two_cabinets_is_offered(two_shops):
    """Совпадение по артикулу — повод предложить, а не связать."""
    found = product_links.suggestions()
    assert found, "одинаковые артикулы двух кабинетов не найдены"
    for item in found:
        assert item["shops"] >= 2
        assert len(item["cards"]) >= 2
    # Пока не подтвердили — карточки остаются сами по себе.
    assert db.query_one("SELECT COUNT(*) AS c FROM product_links")["c"] == 0


def test_one_cabinet_alone_is_not_a_match(two_shops):
    """Товар, который есть только в одном кабинете, сопоставлять не с чем."""
    db.execute(
        "INSERT INTO products(account_id, sku, offer_id, name, barcodes, archived, updated_at) "
        "VALUES(?,?,?,?,?,0,?)",
        (two_shops["ozon"]["id"], "ONLY-HERE", "ART-ONLY", "Товар одного кабинета",
         '["4600000009999"]', db.now_iso()),
    )
    articles = {item["article"] for item in product_links.suggestions()}
    assert "art-only" not in articles


def test_confirmed_match_leaves_the_queue(two_shops):
    """Подтверждённое совпадение больше не просит внимания."""
    article = any_shared_article(two_shops)
    product_links.confirm(article, user={"login": "admin"})
    assert article not in {item["article"] for item in product_links.suggestions()}


def test_confirm_links_all_cards_of_the_article(two_shops):
    article = any_shared_article(two_shops)
    result = product_links.confirm(article, user={"login": "admin"})
    cards = result["cards"]
    assert len({card["account_id"] for card in cards}) >= 2, "связали карточки одного кабинета"
    assert sum(1 for card in cards if card["is_main"]) == 1, "основной товар должен быть один"


def test_main_card_has_barcodes(two_shops):
    """Главной делаем карточку со штрихкодом: её имя и фото уходят в каталог."""
    article = any_shared_article(two_shops)
    cards = product_links.confirm(article, user={"login": "admin"})["cards"]
    main = next(card for card in cards if card["is_main"])
    if any(card["barcodes"] for card in cards):
        assert main["barcodes"], "главной выбрана карточка без штрихкода"


# ------------------------------------------------------------------ отмена
def test_unlink_returns_separate_products(two_shops):
    """«Отменить сопоставление» разводит карточки обратно."""
    article = any_shared_article(two_shops)
    group_id = product_links.confirm(article, user={"login": "admin"})["group_id"]

    removed = product_links.unlink(group_id, user={"login": "admin"})

    assert removed >= 2
    assert product_links.cards_of(group_id) == []
    assert db.query_one("SELECT COUNT(*) AS c FROM product_links WHERE group_id = ?",
                        (group_id,))["c"] == 0


def test_unlink_brings_the_match_back_to_the_queue(two_shops):
    """Отмена говорит «сейчас не связаны», а не «никогда не предлагать»."""
    article = any_shared_article(two_shops)
    group_id = product_links.confirm(article, user={"login": "admin"})["group_id"]
    product_links.unlink(group_id, user={"login": "admin"})
    assert article in {item["article"] for item in product_links.suggestions()}


def test_unlink_keeps_the_goods_themselves(two_shops):
    """Отменили связь — товары остались: их штрихкоды сканируются дальше."""
    article = any_shared_article(two_shops)
    before = db.query_one("SELECT COUNT(*) AS c FROM products WHERE archived = 0")["c"]
    group_id = product_links.confirm(article, user={"login": "admin"})["group_id"]
    product_links.unlink(group_id, user={"login": "admin"})
    assert db.query_one("SELECT COUNT(*) AS c FROM products WHERE archived = 0")["c"] == before


def test_unlink_is_written_to_the_log(two_shops):
    """Связь карточек меняет то, как выглядит каталог, — это событие журнала."""
    article = any_shared_article(two_shops)
    group_id = product_links.confirm(article, user={"login": "admin"})["group_id"]
    product_links.unlink(group_id, user={"login": "admin"})
    assert db.query_one("SELECT 1 FROM events WHERE kind = 'products_unlinked'")


def test_one_card_can_leave_the_group(two_shops):
    """Лишнюю карточку вынимают, не разбирая всю группу."""
    article = any_shared_article(two_shops)
    result = product_links.confirm(article, user={"login": "admin"})
    if len(result["cards"]) < 3:
        # В группе всего две карточки: вынуть одну — значит распустить группу.
        card = next(card for card in result["cards"] if not card["is_main"])
        product_links.unlink_card(card["account_id"], card["sku"], user={"login": "admin"})
        assert product_links.cards_of(result["group_id"]) == []
        return
    card = next(card for card in result["cards"] if not card["is_main"])
    product_links.unlink_card(card["account_id"], card["sku"], user={"login": "admin"})
    left = product_links.cards_of(result["group_id"])
    assert (card["account_id"], card["sku"]) not in {(c["account_id"], c["sku"]) for c in left}
    assert sum(1 for c in left if c["is_main"]) == 1


def test_unlink_of_nothing_is_not_an_error(two_shops):
    assert product_links.unlink("нет-такой-группы") == 0
    assert product_links.unlink_card(999, "нет-такого") is False


# ------------------------------------------------------------------ «не сопоставлять»
def test_skipped_article_is_not_offered(two_shops):
    article = any_shared_article(two_shops)
    product_links.skip(article, user={"login": "admin"})
    assert article not in {item["article"] for item in product_links.suggestions()}
    assert article in {item["article"] for item in product_links.skipped()}


def test_skip_can_be_undone(two_shops):
    article = any_shared_article(two_shops)
    product_links.skip(article, user={"login": "admin"})
    product_links.unskip(article, user={"login": "admin"})
    assert article in {item["article"] for item in product_links.suggestions()}


# ------------------------------------------------------------------ вручную
def test_manual_link_joins_different_articles(two_shops):
    """Артикулы разные — сводим руками: это главный случай ручной работы."""
    ozon, market = two_shops["ozon"], two_shops["market"]
    mine = db.query_one(
        "SELECT sku FROM products WHERE account_id = ? AND archived = 0 LIMIT 1", (ozon["id"],))
    theirs = db.query_one(
        "SELECT sku FROM products WHERE account_id = ? AND archived = 0 LIMIT 1", (market["id"],))
    result = product_links.link(
        (ozon["id"], mine["sku"]), [(market["id"], theirs["sku"])], user={"login": "admin"}
    )
    cards = result["cards"]
    assert len(cards) == 2
    main = next(card for card in cards if card["is_main"])
    assert (main["account_id"], main["sku"]) == (ozon["id"], mine["sku"]), "основным взят не тот"


def test_manual_link_refuses_unknown_card(two_shops):
    ozon = two_shops["ozon"]
    mine = db.query_one("SELECT sku FROM products WHERE account_id = ? LIMIT 1", (ozon["id"],))
    with pytest.raises(ValueError):
        product_links.link((ozon["id"], mine["sku"]), [(ozon["id"], "нет-такого")])


def test_main_can_be_changed(two_shops):
    article = any_shared_article(two_shops)
    cards = product_links.confirm(article, user={"login": "admin"})["cards"]
    other = next(card for card in cards if not card["is_main"])
    assert product_links.set_main(other["account_id"], other["sku"], user={"login": "admin"})
    fresh = product_links.cards_of(other["group_id"] or product_links.group_of(
        other["account_id"], other["sku"]))
    main = next(card for card in fresh if card["is_main"])
    assert (main["account_id"], main["sku"]) == (other["account_id"], other["sku"])


def test_a_card_belongs_to_one_group_only(two_shops):
    """Карточка в двух группах раздвоила бы товар в каталоге."""
    article = any_shared_article(two_shops)
    product_links.confirm(article, user={"login": "admin"})
    doubles = db.query(
        "SELECT account_id, sku, COUNT(*) AS c FROM product_links GROUP BY account_id, sku HAVING c > 1"
    )
    assert not doubles


# ------------------------------------------------------------------ каталог и раздел
def test_linked_product_takes_one_row_in_the_catalog(client, two_shops):
    """Ради этого всё и делалось: одна коробка — одна строка."""
    from app.routes import products as products_routes

    article = any_shared_article(two_shops)
    before = len(products_routes.catalog_rows())
    product_links.confirm(article, user={"login": "admin"})
    after = products_routes.catalog_rows()
    assert len(after) == before - 1, "строк в каталоге не убавилось"
    row = next(row for row in after if row.get("linked"))
    assert row["linked"] >= 2 and len(row["cards"]) == row["linked"]


def test_section_shows_all_cabinets(client, two_shops):
    """Раздел общий: товары обоих кабинетов видно сразу."""
    page = client.get("/products")
    assert page.status_code == 200, page.text
    assert two_shops["ozon"]["title"] in page.text
    assert two_shops["market"]["title"] in page.text


def test_catalog_page_survives_a_linked_product(client, two_shops):
    """Страница с сопоставленным товаром рисует под ним карточки площадок.

    Пустой каталог такую строку не показывает вовсе, поэтому проверяем именно
    сопоставленный: на нём страница один раз и падала.
    """
    article = any_shared_article(two_shops)
    cards = product_links.confirm(article, user={"login": "admin"})["cards"]
    page = client.get("/products")
    assert page.status_code == 200, page.text[:400]
    assert "сопоставлен" in page.text
    assert page.text.count('class="sub"') >= len(cards), "карточек площадок под строкой нет"


def test_search_finds_russian_words_in_any_case(client, two_shops):
    """«кофе» обязано находить «Кофе»: панель русская, и это первое, что пробуют."""
    from app.routes import products as products_routes

    name = db.query_one(
        "SELECT name FROM products WHERE name IS NOT NULL AND name != '' LIMIT 1")["name"]
    word = name.split()[0]
    assert word[0].isupper(), "в подделке нет названия с большой буквы"
    found = products_routes.search(None, word.lower())
    assert found, f"поиск по «{word.lower()}» ничего не нашёл"
    assert any(word.lower() in (item["name"] or "").lower() for item in found)


def test_cabinet_filter_narrows_the_list(client, two_shops):
    """Фильтр по кабинетам: выбрали один — чужих товаров в списке нет."""
    from app.routes import products as products_routes

    only_market = products_routes.catalog_rows([two_shops["market"]["id"]])
    assert only_market
    assert {row["account_id"] for row in only_market} == {two_shops["market"]["id"]}


def test_match_tab_offers_and_confirms(client, two_shops):
    """Путь целиком, как его проходит человек: увидел — подтвердил — отменил."""
    page = client.get("/products?tab=match&view=new")
    assert page.status_code == 200, page.text
    assert "совпал артикул" in page.text

    article = any_shared_article(two_shops)
    confirmed = client.post("/api/products/links/confirm", json={"article": article})
    assert confirmed.status_code == 200, confirmed.text

    linked = client.get("/products?tab=match&view=linked")
    assert "Отменить сопоставление" in linked.text

    group_id = product_links.groups()[0]["group_id"]
    undone = client.request("DELETE", f"/api/products/links/{group_id}")
    assert undone.status_code == 200, undone.text
    assert "вернётся в предложения" in undone.json()["message"]
    assert product_links.cards_of(group_id) == []


def test_confirm_all_takes_everything(client):
    """«Подтвердить все» — для тех, у кого артикулы ведутся аккуратно."""
    waiting = len(product_links.suggestions())
    assert waiting, "нечего подтверждать"
    response = client.post("/api/products/links/confirm", json={"all": True})
    assert response.status_code == 200, response.text
    assert response.json()["linked"] == waiting
    assert product_links.suggestions() == []


def test_matching_is_admin_only(client):
    """Связь карточек меняет каталог — сборщику такое не доверяем."""
    from app.core.security import hash_password

    db.execute(
        "INSERT INTO users(login, password_hash, role, active, created_at) VALUES(?,?,?,1,?)",
        ("packer7", hash_password("packer123456"), "packer", db.now_iso()),
    )
    client.post("/login", data={"login": "packer7", "password": "packer123456"})
    assert client.post("/api/products/links/confirm", json={"all": True}).status_code == 403
    assert client.request("DELETE", "/api/products/links/любая").status_code == 403


def test_matching_requires_csrf(client):
    del client.headers["X-CSRF-Token"]
    assert client.post("/api/products/links/confirm", json={"all": True}).status_code == 403


def test_deleting_a_cabinet_takes_its_links(two_shops):
    """Кабинет удалили — его карточки и связи уходят с ним."""
    article = any_shared_article(two_shops)
    product_links.confirm(article, user={"login": "admin"})
    market = two_shops["market"]
    accounts.delete(market["id"], user={"login": "admin"})
    assert db.query_one(
        "SELECT COUNT(*) AS c FROM product_links WHERE account_id = ?", (market["id"],)
    )["c"] == 0


def test_sync_does_not_link_anything_by_itself(two_shops):
    """Молчаливое сопоставление — худшее, что может сделать панель."""
    sync.sync_all()
    assert db.query_one("SELECT COUNT(*) AS c FROM product_links")["c"] == 0
