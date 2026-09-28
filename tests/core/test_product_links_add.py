"""Добавить карточку в уже сопоставленный товар.

Товар сопоставили с одним кабинетом, а потом он появился ещё на одной площадке
или в соседнем магазине. Разбирать сопоставление ради этого незачем: у группы
есть «+ Добавить карточку». Основной товар остаётся прежним, штрихкоды новой
карточки сразу годятся к заказам всей группы. Карточку из другой группы панель
молча не переносит — это слило бы два товара в один.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app.core import catalog, db, linked, product_links
from app.main import app


def put(account, sku, name, barcodes=()):
    with db.write() as conn:
        catalog.save(conn, account["id"], [{"sku": sku, "offer_id": sku, "name": name, "barcodes": list(barcodes)}])
    return account["id"], sku


@pytest.fixture
def group(account, yandex_account, avito_account):
    """Кружка сопоставлена между Ozon и Маркетом; объявление Avito — пока отдельно."""
    oz = put(account, "OZ-MUG", "Кружка", ["4600000000111"])
    ya = put(yandex_account, "YA-MUG", "Кружка Маркета", ["4650000000222"])
    av = put(avito_account, "777", "Кружка на Avito")
    group_id = product_links.link(oz, [ya], user={"login": "admin"})["group_id"]
    return {"id": group_id, "oz": oz, "ya": ya, "av": av}


@pytest.fixture
def client(group):
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.post("/login", data={"login": "admin", "password": "test-admin-pass"})
        page = test_client.get("/products?tab=match&view=linked")
        test_client.headers["X-CSRF-Token"] = re.search(
            r'name="csrf-token" content="([^"]*)"', page.text).group(1)
        yield test_client


def members(group_id):
    return {(card["account_id"], card["sku"]): card["is_main"] for card in product_links.cards_of(group_id)}


def test_card_joins_the_group_and_the_main_stays(group):
    result = product_links.add_to_group(group["id"], [group["av"]], user={"login": "admin"})
    assert result["added"] == 1
    assert members(group["id"]) == {group["oz"]: True, group["ya"]: False, group["av"]: False}
    # Штрихкоды группы сразу годятся и объявлению Avito.
    assert set(linked.extras(*group["av"])[0]) == {"4600000000111", "4650000000222"}


def test_card_of_another_group_is_not_moved_silently(group, account, avito_account):
    other = product_links.link(put(account, "OZ-CUP", "Чашка"), [group["av"]], user={"login": "admin"})["group_id"]
    with pytest.raises(ValueError, match="уже сопоставлена с товаром «Чашка»"):
        product_links.add_to_group(group["id"], [group["av"]])
    assert group["av"] in members(other), "карточка осталась в своей группе"
    assert group["av"] not in members(group["id"])


def test_card_already_inside_is_nothing_to_add(group):
    with pytest.raises(ValueError, match="Добавлять нечего"):
        product_links.add_to_group(group["id"], [group["ya"]])


def test_unknown_group_or_card_is_refused(group, avito_account):
    with pytest.raises(ValueError, match="Такого сопоставления нет"):
        product_links.add_to_group("нет-такой", [group["av"]])
    with pytest.raises(ValueError, match="нет в каталоге"):
        product_links.add_to_group(group["id"], [(avito_account["id"], "нет-такого")])


# ------------------------------------------------------------------ страница и API
def test_linked_group_is_folded_with_add_next_to_unlink(client, group):
    """Группа свёрнута; «+ Добавить карточку» — в шапке рядом с «Отменить сопоставление».

    «+» группу не раскрывает: поиск добавления стоит под шапкой отдельно от
    списка карточек, а карточки раскрываются щелчком по названию.
    """
    page = client.get("/products?tab=match&view=linked").text
    block = page[page.index(f'id="group-{group["id"]}"'):]
    head = block[:block.index("data-add-box")]
    assert f'data-add-open="{group["id"]}"' in head and f'data-unlink="{group["id"]}"' in head
    assert head.index("data-add-open") < head.index("data-unlink"), "«+» рядом, перед отменой"
    assert "data-group-toggle" in head, "раскрывает название, а не «+»"
    assert "Маркет" in head and "Ozon" in head, "в свёрнутой шапке видно площадки"
    cards = block[block.index("data-group-cards"):]
    assert cards.startswith("data-group-cards hidden"), "карточки по умолчанию скрыты"
    assert block.index("data-add-box") < block.index("data-group-cards"), "поиск «+» — вне списка карточек"


def test_card_is_added_through_the_api(client, group):
    response = client.post(f"/api/products/links/{group['id']}/add",
                           json={"cards": [{"account_id": group["av"][0], "sku": group["av"][1]}]})
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "Добавлено в сопоставление: 1. Карточек теперь 3"
    assert group["av"] in members(group["id"])
    refused = client.post(f"/api/products/links/{group['id']}/add",
                          json={"cards": [{"account_id": group["ya"][0], "sku": group["ya"][1]}]})
    assert refused.status_code == 400 and "Добавлять нечего" in refused.json()["detail"]


def test_adding_needs_csrf_and_a_manager(client, group):
    body = {"cards": [{"account_id": group["av"][0], "sku": group["av"][1]}]}
    token = client.headers.pop("X-CSRF-Token")
    assert client.post(f"/api/products/links/{group['id']}/add", json=body).status_code == 403
    client.headers["X-CSRF-Token"] = token

    from app.core.security import hash_password

    db.execute("INSERT INTO users(login, password_hash, role, active, created_at) VALUES(?,?,?,1,?)",
               ("packer8", hash_password("packer123456"), "packer", db.now_iso()))
    client.post("/login", data={"login": "packer8", "password": "packer123456"})
    assert client.post(f"/api/products/links/{group['id']}/add", json=body).status_code == 403
    assert group["av"] not in members(group["id"])
