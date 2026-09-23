"""Каталог товаров Яндекс Маркета: раздел «Товары» для его кабинетов.

Раньше каталог со штрихкодами отдавал только Ozon, и кабинет Маркета в раздел
не пускали вовсе. Но Маркет отдаёт карточку целиком одним методом
(offer-mappings), а сборщику штрихкод нужен независимо от того, чей это заказ.

Ключ товара у Маркета — артикул продавца (offerId): он есть всегда, а marketSku
появляется только после привязки к карточке Маркета.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app.core import catalog, db, product_sets
from app.main import app
from app.markets.yandex import catalog as yandex_catalog
from app.markets.yandex import client as yandex
from app.markets.yandex import sync as yandex_sync
from app.markets.yandex.client import YandexError
from tests.fakes import CATALOG_ARCHIVED, CATALOG_EXTRA, SAMPLE_PRODUCTS


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
        switched = test_client.post(
            "/api/account/switch", json={"account_id": market["id"], "next": "/yandex"}
        )
        assert switched.status_code == 200, switched.text
        yield test_client


def live(account) -> set[str]:
    return {row["sku"] for row in db.query(
        "SELECT sku FROM products WHERE account_id = ? AND archived = 0", (account["id"],))}


def stored(account) -> set[str]:
    return {row["sku"] for row in db.query(
        "SELECT sku FROM products WHERE account_id = ?", (account["id"],))}


# ------------------------------------------------------------------ обход
def test_refresh_loads_the_whole_catalog(market):
    """Кнопка в разделе «Товары» приносит весь каталог кабинета, а не только заказанное."""
    result = catalog.refresh(market)
    for _sku, offer, _name, _bc in CATALOG_EXTRA:
        assert offer in live(market), "товар каталога Маркета не загрузился"
    assert result["live"] == len(live(market))
    assert result["saved"] >= len(CATALOG_EXTRA)


def test_offer_id_is_the_key(market):
    """Ключом товара берём артикул продавца — по нему сборщик находит позицию заказа."""
    catalog.refresh(market)
    row = db.query_one(
        "SELECT sku, offer_id FROM products WHERE account_id = ? AND sku = ?",
        (market["id"], SAMPLE_PRODUCTS[0][1]),
    )
    assert row, "товар не найден по артикулу продавца"
    assert row["offer_id"] == row["sku"]


def test_barcodes_are_saved_for_scanning(market):
    """Ради штрихкодов всё и затевалось: без них сканировать нечего."""
    catalog.refresh(market)
    _sku, offer, _name, barcode = SAMPLE_PRODUCTS[0]
    row = db.query_one(
        "SELECT sku FROM product_barcodes WHERE account_id = ? AND barcode = ?",
        (market["id"], barcode),
    )
    assert row and row["sku"] == offer


def test_archive_does_not_become_catalog(market):
    """Архивный товар Маркет и не отдаёт, но проверку у себя не снимаем."""
    catalog.refresh(market)
    for _sku, offer, _name, _bc in CATALOG_ARCHIVED:
        assert offer not in live(market), "архивный товар попал в каталог"


def test_archived_card_is_filtered_even_if_it_arrives(market):
    """Маркет прислал архивную карточку мимо фильтра — в каталог она не идёт."""
    card = {"offer": {"offerId": "ART-999", "name": "Снято с продажи", "archived": True}}
    assert yandex_catalog.is_archived(card) is True
    assert yandex_catalog.is_archived({"offer": {"offerId": "ART-001"}}) is False


def test_card_without_an_article_is_skipped():
    """Без артикула карточка бесполезна: ключа у товара не будет."""
    assert yandex_catalog.card({"offer": {"name": "Без артикула"}}) is None
    assert yandex_catalog.card({})is None


def test_picture_comes_from_uploaded_files_too():
    """Картинка может лежать не ссылкой, а загруженным файлом — берём и её."""
    card = yandex_catalog.card({"offer": {
        "offerId": "ART-777",
        "mediaFiles": {"pictures": [{"url": "https://example/1.jpg", "uploadState": "UPLOADED"}]},
    }})
    assert card["image"] == "https://example/1.jpg"


def test_product_that_left_the_catalog_goes_to_archive(market):
    """Товар убрали с Маркета — из раздела он уходит, из базы нет.

    Удалять нельзя: по его штрихкоду могут собирать заказ, который уже в работе.
    """
    catalog.refresh(market)
    client = yandex.get_client(market)
    gone = client.catalog[0][0]
    client.catalog = client.catalog[1:]
    catalog.refresh(market)

    assert gone not in live(market), "исчезнувший товар остался в каталоге"
    assert gone in stored(market), "строка товара удалена — штрихкод перестанет сканироваться"


def test_refresh_survives_a_second_run(market):
    """Повтор не плодит дублей и не отправляет каталог в архив."""
    first = catalog.refresh(market)
    second = catalog.refresh(market)
    assert second["live"] == first["live"]
    assert second["archived_marked"] == 0


def test_walk_stops_on_the_last_page(market):
    """Страницы кончились — обход заканчивается, а не крутится до предела."""
    client = yandex.get_client(market)
    calls = []
    original = client.offer_mappings

    def counted(**kwargs):
        calls.append(kwargs.get("page_token"))
        return original(**kwargs)

    client.offer_mappings = counted
    list(yandex_catalog.pages(market))
    client.offer_mappings = original
    assert calls, "каталог не запрашивался вовсе"
    assert len(calls) < yandex_catalog.MAX_PAGES


def test_failure_is_reported_not_swallowed(market):
    """Маркет отказал — человек должен это увидеть, а не гадать, почему пусто."""
    client = yandex.get_client(market)

    def broken(**_kwargs):
        raise YandexError("Маркет отказал", status=403)

    client.offer_mappings = broken
    with pytest.raises(YandexError):
        catalog.refresh(market)


# ------------------------------------------------- карточки по заказам, без кнопки
def test_sync_brings_cards_of_ordered_goods(yandex_account):
    """Товары из заказов приезжают сами: сканировать надо сегодня, а не после обхода."""
    result = yandex_sync.sync_account(yandex_account)
    assert result["yandex_products"] > 0
    ordered = db.query(
        "SELECT DISTINCT offer_id FROM yandex_order_items WHERE account_id = ?",
        (yandex_account["id"],),
    )
    assert ordered, "в подделке Маркета нет позиций заказов"
    for row in ordered:
        assert row["offer_id"] in stored(yandex_account), "карточка заказанного товара не загрузилась"


def test_second_sync_does_not_ask_again(yandex_account):
    """Второй проход не дёргает Маркет впустую: карточки уже есть."""
    yandex_sync.sync_account(yandex_account)
    again = yandex_sync.sync_products(yandex_account)
    assert again["yandex_products"] == 0


def test_an_article_without_a_card_is_asked_only_once(yandex_account):
    """Товара нет в каталоге Маркета — спрашиваем один раз, а не каждую минуту."""
    yandex_sync.sync_yandex(yandex_account)
    client = yandex.get_client(yandex_account)
    client.catalog = []          # Маркет не знает ни одного из заказанных артикулов
    client.archived = []
    calls = []
    original = client.offer_mappings

    def counted(**kwargs):
        calls.append(kwargs.get("offer_ids"))
        return original(**kwargs)

    client.offer_mappings = counted
    first = yandex_sync.sync_products(yandex_account)
    second = yandex_sync.sync_products(yandex_account)
    client.offer_mappings = original

    assert first["yandex_products"] == 0 and second["yandex_products"] == 0
    assert len(calls) == 1, "второй проход снова пошёл в Маркет за тем же"
    row = db.query_one(
        "SELECT name, barcodes FROM products WHERE account_id = ? LIMIT 1", (yandex_account["id"],)
    )
    assert row and row["name"], "у пустой строки нет даже названия из заказа"


# ------------------------------------------------------------------ раздел
def test_products_section_opens_for_a_market_cabinet(client, market):
    """Раньше кабинету Маркета раздел отвечал 409 — каталога у площадки не было."""
    catalog.refresh(market)
    page = client.get("/products")
    assert page.status_code == 200, page.text
    assert SAMPLE_PRODUCTS[0][2] in page.text, "товара Маркета нет на странице"


def test_the_article_is_not_printed_twice(client, market):
    """Ключ товара Маркета и есть его артикул — в колонке SKU он не повторяется."""
    catalog.refresh(market)
    page = client.get("/products")
    article = SAMPLE_PRODUCTS[0][1]
    twice = re.search(
        rf">{article}</td>\s*(<!--.*?-->\s*)?<td[^>]*>{article}</td>", page.text, re.S
    )
    assert not twice, "артикул продавца показан и в «Артикуле», и в «SKU»"
    assert f">{article}</td>" in page.text, "артикула нет в таблице вовсе"


def test_menu_shows_the_section_on_a_market_cabinet(client):
    """Раздел бесполезен, если в него не ведёт пункт меню."""
    page = client.get("/yandex/pack")
    assert page.status_code == 200, page.text
    assert 'href="/products"' in page.text, "в шапке кабинета Маркета нет «Товаров»"


def test_refresh_button_works_for_a_market_cabinet(client):
    """Кнопка «Обновить каталог» на кабинете Маркета не отказывает."""
    started = client.post("/api/products/catalog/refresh")
    assert started.status_code == 200, started.text
    assert started.json()["status"] in ("started", "running")


def test_sets_are_made_of_market_goods(client, market):
    """Набор собирается и из товаров Маркета: складу всё равно, чей это артикул."""
    catalog.refresh(market)
    goods = sorted(live(market))
    saved = product_sets.save(
        market["id"], goods[0], [{"sku": goods[1], "quantity": 2}], user={"login": "admin"}
    )
    assert saved["parts"][0]["quantity"] == 2
    page = client.get("/products?tab=sets")
    assert page.status_code == 200
