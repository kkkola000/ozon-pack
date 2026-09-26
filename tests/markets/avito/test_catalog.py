"""Каталог Avito: объявления кабинета через «Получение информации по объявлениям».

Метод один — GET /core/v1/items. Из объявления берутся только номер (он же
артикул) и название: фото и штрихкодов метод не отдаёт, и панель их не
придумывает.
"""
import httpx
import pytest

from app.core import catalog as core_catalog
from app.core import db
from app.markets.avito import catalog as avito_catalog
from app.markets.avito import client as avito
from app.markets.avito.client import AvitoClient


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(avito_catalog, "PAUSE", 0)


def test_request_follows_the_documented_method():
    seen = []

    def handler(request):
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 86400})
        seen.append((request.method, request.url.path, dict(request.url.params), request.headers.get("authorization")))
        return httpx.Response(200, json={"meta": {"page": 2, "per_page": 99},
                                         "resources": [{"id": 24122231, "title": "Кеды", "status": "active"}]})

    client = AvitoClient("id", "secret", "https://api.avito.ru", max_retries=1)
    client._client = httpx.Client(base_url=client.base_url, transport=httpx.MockTransport(handler))
    items = client.items(page=2)
    assert seen == [("GET", "/core/v1/items", {"page": "2", "per_page": "99", "status": "active"}, "Bearer t")]
    assert items == [{"id": 24122231, "title": "Кеды", "status": "active"}]


def test_card_is_number_as_article_and_title_only():
    card = avito_catalog.card({"id": 24122231, "title": "Кеды Venice, 42", "price": 4990, "status": "active",
                               "url": "https://www.avito.ru/x", "category": {"id": 111, "name": "Товары"}})
    assert card == {"sku": "24122231", "offer_id": "24122231", "name": "Кеды Venice, 42", "image": None,
                    "barcodes": []}
    assert avito_catalog.card({"title": "без номера"}) is None


def test_only_active_items_go_to_the_catalog(avito_account):
    pages = list(avito_catalog.pages(avito_account))
    cards = [card for page in pages for card in page.items]
    names = {card["name"] for card in cards}
    assert "Термос 1 л" in names, "активное объявление без заказов — тоже товар кабинета"
    assert "Самокат детский" not in names, "снятое объявление в каталог не идёт"
    assert sum(page.skipped for page in pages) == 1
    assert all(card["image"] is None and card["barcodes"] == [] for card in cards)
    assert all(card["offer_id"] == card["sku"] for card in cards)


def test_pages_are_walked_until_a_short_one(avito_account, monkeypatch):
    monkeypatch.setattr(avito, "ITEMS_PER_PAGE", 3)
    fake = avito.get_client(avito_account)
    pages = list(avito_catalog.pages(avito_account))
    assert [request[0] for request in fake.item_requests] == [1, 2, 3]
    assert all(request[1] == 3 for request in fake.item_requests)
    assert sum(len(page.items) for page in pages) == 7


def test_refresh_fills_the_products_of_the_cabinet(avito_account):
    result = core_catalog.refresh(avito_account)
    assert result["live"] == 7
    rows = db.query("SELECT sku, offer_id, name, image, barcodes FROM products WHERE account_id = ? ORDER BY sku",
                    (avito_account["id"],))
    assert {row["name"] for row in rows} >= {"Кеды Venice, 42", "Термос 1 л"}
    assert all(row["offer_id"] == row["sku"] and row["image"] is None and row["barcodes"] == "[]" for row in rows)
    assert not db.query_one("SELECT 1 FROM product_barcodes WHERE account_id = ?", (avito_account["id"],))
