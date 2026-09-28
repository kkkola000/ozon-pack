"""«Товары»: поиск в каждой вкладке — каталог, наборы и комплекты, сопоставление.

У каждой вкладки своя строка поиска с тем же видом, что у каталога. В
сопоставлении запрос один на все три списка и не теряется при переходе между
ними. Регистр не важен и у кириллицы, штрихкод ищется целиком по справочнику.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app.core import catalog, db, product_links, product_sets
from app.main import app
from tests.conftest import pick_posting


def put_card(account, sku, name, *, barcodes=(), offer_id=None):
    with db.write() as conn:
        catalog.save(conn, account["id"], [{"sku": sku, "offer_id": offer_id or sku, "name": name,
                                             "barcodes": list(barcodes)}])
    return account["id"], str(sku)


@pytest.fixture
def client(sample_data):
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.post("/login", data={"login": "admin", "password": "test-admin-pass"})
        yield test_client


def page(client, **params) -> str:
    response = client.get("/products", params=params)
    assert response.status_code == 200, response.text
    return response.text


def form(text: str, tab: str) -> str:
    match = re.search(r'<form class="row products-search".*?</form>', text, re.S)
    assert match, "у вкладки нет строки поиска"
    assert f'name="tab" value="{tab}"' in match.group(0)
    return match.group(0)


# ------------------------------------------------------------------ каждая вкладка
@pytest.mark.parametrize("tab, view", [("catalog", ""), ("sets", ""), ("match", "new"),
                                       ("match", "linked"), ("match", "skipped")])
def test_every_tab_has_a_search(client, tab, view):
    text = page(client, tab=tab, view=view) if view else page(client, tab=tab)
    box = form(text, tab)
    assert 'type="search" name="q"' in box and ">Найти</button>" in box
    if view:
        assert f'name="view" value="{view}"' in box, "поиск остаётся в той же подвкладке"


# ------------------------------------------------------------------ наборы и комплекты
@pytest.fixture
def two_sets(account, sample_data):
    first = pick_posting(positions=1)["items"][0]["sku"]
    second = pick_posting(positions=2)["items"][0]["sku"]
    product_sets.save(account["id"], first, [{"barcode": "9990000000771", "title": "Подарочный пакет"}],
                      title="Кофейный набор")
    product_sets.save(account["id"], second, [{"barcode": "9990000000788", "title": "Чехол"}],
                      title="Кружка с чехлом", kind="kit")
    return first, second


def test_sets_are_found_by_title_part_and_barcode(client, two_sets):
    for query in ("кофейный", "ПАКЕТ", "9990000000771"):
        text = page(client, tab="sets", q=query)
        assert "Кофейный набор" in text and "Кружка с чехлом" not in text, query
    kit = page(client, tab="sets", q="чехол")
    assert "Кружка с чехлом" in kit and "Кофейный набор" not in kit


def test_sets_search_says_when_nothing_is_found(client, two_sets):
    text = page(client, tab="sets", q="нет-такого")
    assert "Ничего не нашлось по запросу «нет-такого»" in text
    # Число на вкладке — все наборы, а не найденные.
    assert re.search(r'Наборы и комплекты <span class="badge">2</span>', text)


# ------------------------------------------------------------------ сопоставление
@pytest.fixture
def market_cards(yandex_account, sample_data):
    """Карточки Маркета с артикулами Ozon — совпадения ждут решения."""
    rows = db.query("SELECT offer_id, name FROM products WHERE account_id = 1 AND offer_id IS NOT NULL "
                    "ORDER BY offer_id LIMIT 3")
    for row in rows:
        put_card(yandex_account, row["offer_id"], row["name"], offer_id=row["offer_id"])
    return [dict(row) for row in rows]


def test_suggestions_are_found_and_the_query_follows_the_sub_tabs(client, market_cards):
    wanted, other = market_cards[0], market_cards[1]
    text = page(client, tab="match", view="new", q=wanted["name"].upper())
    assert wanted["offer_id"].lower() in text.lower()
    assert f'data-confirm="{other["offer_id"].lower()}"' not in text
    link =re.search(r'href="(/products\?tab=match[^"]*view=linked[^"]*)"', text).group(1)
    assert "q=" in link, "переход в «Сопоставленные» не теряет запрос"
    assert "Ничего не нашлось" in page(client, tab="match", view="new", q="нет-такого")


def test_linked_groups_are_found_without_case_and_by_barcode(client, account, yandex_account, sample_data):
    main = put_card(yandex_account, "YA-MUG", "Термокружка Походная")
    put_card(account, "OZ-MUG", "Термокружка", barcodes=["4600000000999"])
    product_links.link(main, [(account["id"], "OZ-MUG")], user={"login": "admin"})
    for query in ("термокружка походная", "4600000000999", "ya-mug"):
        text = page(client, tab="match", view="linked", q=query)
        assert 'data-unlink="' in text and "Термокружка Походная" in text, query
    assert "Ничего не нашлось" in page(client, tab="match", view="linked", q="нет-такого")


def test_skipped_articles_are_found(client, market_cards):
    first, second = market_cards[0], market_cards[1]
    for item in (first, second):
        product_links.skip(item["offer_id"], user={"login": "admin"})
    text = page(client, tab="match", view="skipped", q=first["offer_id"])
    assert f'data-unskip="{first["offer_id"].lower()}"' in text
    assert f'data-unskip="{second["offer_id"].lower()}"' not in text
