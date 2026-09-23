"""Наборы: товар площадки, который на складе собирают из нескольких вещей.

На Ozon набор — обычный товар: один SKU, один штрихкод, одна позиция в
отправлении. Но наклейки набора на полке нет — сборщик берёт его части и
сканирует их. Раньше на такой скан панель отвечала «СТОП: товар не из этого
отправления»: SKU части в составе отправления не значится.

Главное, что здесь проверяется: часть набора засчитывается в позицию набора, а
набор закрывается только тогда, когда набраны все его части. Закрыть позицию по
одной части нельзя — в коробку уедет половина комплекта.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app.core import db, product_sets
from app.markets.ozon import pack as packing
from app.main import app
from tests.conftest import account_id, barcode_of, pick_posting
from app.markets.ozon import sync as ozon_sync
from app.markets.ozon import store as ozon_store


def make_set(account, sku, parts, title="Набор"):
    """Завести набор напрямую — как это делает раздел «Товары»."""
    return product_sets.save(account["id"], sku, parts, title=title, user={"login": "admin"})


def other_products(account, exclude, count=2):
    """Товары каталога, не попавшие в отправление, — годятся в части набора."""
    placeholders = ",".join("?" for _ in exclude)
    rows = db.query(
        f"SELECT sku, name FROM products WHERE account_id = ? AND sku NOT IN ({placeholders}) "
        f"AND sku IN (SELECT sku FROM product_barcodes WHERE account_id = ?) LIMIT ?",
        [account["id"]] + list(exclude) + [account["id"], count],
    )
    assert len(rows) >= count, "в подделке мало товаров для состава набора"
    return [dict(row) for row in rows]


@pytest.fixture
def set_posting(account, sample_data, user):
    """Взятое в работу отправление, позиция которого объявлена набором.

    Отправление берём явно: часть набора бывает и самостоятельным товаром, и
    тогда под её скан подходит не одно отправление — какое взять, решает
    человек. Здесь проверяется учёт, а не подбор.
    """
    posting = pick_posting(positions=1)
    sku = posting["items"][0]["sku"]
    parts = other_products(account, [sku], 2)
    make_set(account, sku, [{"sku": part["sku"], "quantity": 1} for part in parts])
    packing.select_posting(account, user, posting["posting_number"])
    return {"posting": posting, "sku": sku, "parts": parts}


# ------------------------------------------------------------------ состав
def test_set_is_an_ordinary_product_with_a_composition(account, sample_data):
    """Набор — обычный товар площадки, состав живёт только в панели."""
    posting = pick_posting(positions=1)
    sku = posting["items"][0]["sku"]
    parts = other_products(account, [sku], 2)

    saved = make_set(account, sku, [{"sku": parts[0]["sku"], "quantity": 2},
                                    {"sku": parts[1]["sku"], "quantity": 1}])
    assert saved["sku"] == sku
    assert [part["quantity"] for part in saved["parts"]] == [2, 1]
    # Товар в каталоге не изменился: панель площадке ничего не отправляет.
    row = db.query_one("SELECT * FROM products WHERE account_id = ? AND sku = ?", (account["id"], sku))
    assert row is not None


def test_part_without_a_product_is_kept_by_barcode(account, sample_data):
    """Не всё в наборе продаётся отдельно: вкладыш, пакет, подарок."""
    sku = pick_posting(positions=1)["items"][0]["sku"]
    saved = make_set(account, sku, [{"barcode": "9990000000017", "title": "Подарочный пакет"}])
    part = saved["parts"][0]
    assert part["part_sku"] is None
    assert part["barcode"] == "9990000000017"
    assert part["part_key"].startswith(product_sets.BARCODE_PREFIX)


def test_known_barcode_is_linked_to_its_product(account, sample_data):
    """Штрихкод знакомого товара — это товар, а не голый код на экране."""
    sku = pick_posting(positions=1)["items"][0]["sku"]
    part_product = other_products(account, [sku], 1)[0]
    saved = make_set(account, sku, [{"barcode": barcode_of(part_product["sku"])}])
    assert saved["parts"][0]["part_sku"] == part_product["sku"]
    assert saved["parts"][0]["title"] == part_product["name"]


def test_same_part_twice_is_a_quantity(account, sample_data):
    """Две строки про одно и то же — это «нужно две штуки»."""
    sku = pick_posting(positions=1)["items"][0]["sku"]
    part = other_products(account, [sku], 1)[0]
    saved = make_set(account, sku, [{"sku": part["sku"], "quantity": 1},
                                    {"sku": part["sku"], "quantity": 2}])
    assert len(saved["parts"]) == 1 and saved["parts"][0]["quantity"] == 3


def test_set_cannot_contain_itself(account, sample_data):
    sku = pick_posting(positions=1)["items"][0]["sku"]
    with pytest.raises(product_sets.SetError, match="самого себя"):
        make_set(account, sku, [{"sku": sku, "quantity": 1}])


def test_nested_sets_are_refused(account, sample_data):
    """Вложенные наборы молча считались бы неправильно — лучше отказать."""
    first = pick_posting(positions=1)["items"][0]["sku"]
    inner, outer = other_products(account, [first], 2)
    make_set(account, inner["sku"], [{"barcode": "9990000000024"}])
    with pytest.raises(product_sets.SetError, match="сам является набором"):
        make_set(account, outer["sku"], [{"sku": inner["sku"], "quantity": 1}])


def test_empty_composition_is_refused(account, sample_data):
    sku = pick_posting(positions=1)["items"][0]["sku"]
    with pytest.raises(product_sets.SetError, match="хотя бы одна часть"):
        make_set(account, sku, [])


def test_set_needs_a_real_product(account, sample_data):
    with pytest.raises(product_sets.SetError, match="нет в каталоге"):
        make_set(account, "нет-такого-sku", [{"barcode": "9990000000031"}])


def test_saving_replaces_the_whole_composition(account, sample_data):
    """Состав пишется целиком: половины от прошлой версии остаться не должно."""
    sku = pick_posting(positions=1)["items"][0]["sku"]
    parts = other_products(account, [sku], 2)
    make_set(account, sku, [{"sku": part["sku"], "quantity": 1} for part in parts])
    saved = make_set(account, sku, [{"sku": parts[1]["sku"], "quantity": 4}])
    assert [(p["part_sku"], p["quantity"]) for p in saved["parts"]] == [(parts[1]["sku"], 4)]


def test_deleting_a_set_leaves_the_product(account, sample_data):
    sku = pick_posting(positions=1)["items"][0]["sku"]
    make_set(account, sku, [{"barcode": "9990000000048"}])
    assert product_sets.delete(account["id"], sku, {"login": "admin"}) is True
    assert product_sets.get(account["id"], sku) is None
    assert db.query_one("SELECT sku FROM products WHERE account_id = ? AND sku = ?",
                        (account["id"], sku)) is not None


# ------------------------------------------------------------------ сборка
def test_scanning_a_part_counts_towards_the_set(account, set_posting, user):
    """Ради этого всё и делается: скан части — работа по позиции набора.

    Раньше здесь был «СТОП: товар не из этого отправления»: SKU части в
    составе отправления не значится, там стоит один SKU набора.
    """
    sku, parts = set_posting["sku"], set_posting["parts"]
    item = next(i for i in packing.load_state(account, user)["items"] if i["sku"] == sku)
    assert item["is_set"] is True, "позиция не опознана как набор"

    result = packing.scan(account, user, barcode_of(parts[0]["sku"]))
    assert result["action"] == "set_part_scanned", result["message"]
    assert "СТОП" not in result["message"]


def test_set_closes_only_when_every_part_is_scanned(account, set_posting, user):
    """По одной части позицию закрыть нельзя: уедет половина комплекта."""
    sku, parts = set_posting["sku"], set_posting["parts"]
    packing.scan(account, user, barcode_of(parts[0]["sku"]))

    state = packing.load_state(account, user)
    item = next(i for i in state["items"] if i["sku"] == sku)
    assert item["scanned"] == 0, "набор закрылся по одной части"
    assert [p["ok"] for p in item["parts"]] == [True, False]

    packing.scan(account, user, barcode_of(parts[1]["sku"]))
    item = next(i for i in packing.load_state(account, user)["items"] if i["sku"] == sku)
    assert item["scanned"] >= 1 and item["ok"] is True


def test_part_quantity_is_respected(account, sample_data, user):
    """Две штуки на набор — значит два скана, а не один."""
    posting = pick_posting(positions=1)
    sku = posting["items"][0]["sku"]
    part = other_products(account, [sku], 1)[0]
    make_set(account, sku, [{"sku": part["sku"], "quantity": 2}])
    packing.select_posting(account, user, posting["posting_number"])

    packing.scan(account, user, barcode_of(part["sku"]))
    item = next(i for i in packing.load_state(account, user)["items"] if i["sku"] == sku)
    assert item["parts"][0]["need"] == 2 * item["need"]
    assert item["scanned"] == 0, "набор закрылся при половине нужного количества"

    # Вторая штука той же части — вот теперь один набор собран.
    packing.scan(account, user, barcode_of(part["sku"]))
    item = next(i for i in packing.load_state(account, user)["items"] if i["sku"] == sku)
    assert item["scanned"] == 1


def test_extra_part_is_refused_not_counted(account, set_posting, user):
    """Лишний скан части — предупреждение, а не тихий плюс."""
    sku, parts = set_posting["sku"], set_posting["parts"]
    need = next(i for i in packing.load_state(account, user)["items"] if i["sku"] == sku)["need"]
    for _ in range(need):
        packing.scan(account, user, barcode_of(parts[0]["sku"]))

    result = packing.scan(account, user, barcode_of(parts[0]["sku"]))
    assert result["action"] == "extra_product"
    assert result["status"] == "warning"


def test_part_barcode_without_a_product_is_accepted(account, sample_data, user):
    """Часть без товара в каталоге раньше была «код не распознан»."""
    posting = pick_posting(positions=1)
    sku = posting["items"][0]["sku"]
    make_set(account, sku, [{"barcode": "9990000000055", "title": "Вкладыш"}])

    # Свободное место: у штрихкода нет своего товара, и подобрать отправление
    # панель может только через набор, в который он входит.
    result = packing.scan(account, user, "9990000000055")
    assert result["action"] == "posting_selected", result["message"]
    assert "не найден" not in result["message"]
    # Скан, которым взяли отправление, засчитан как часть, а не как весь набор.
    item = next(i for i in result["state"]["items"] if i["sku"] == sku)
    assert item["parts"][0]["scanned"] == 1
    assert item["scanned"] == min(1, item["need"])


def test_foreign_product_still_stops_the_packer(account, set_posting, user):
    """Наборы не должны превращать «СТОП» в «наверное, это часть»."""
    sku = set_posting["sku"]
    stranger = db.query_one(
        "SELECT sku FROM products WHERE account_id = ? AND sku NOT IN "
        "(SELECT part_sku FROM product_set_items WHERE account_id = ? AND part_sku IS NOT NULL) "
        "AND sku != ? AND sku IN (SELECT sku FROM product_barcodes WHERE account_id = ?) LIMIT 1",
        (account["id"], account["id"], sku, account["id"]),
    )
    if not stranger:
        pytest.skip("в подделке нет чужого товара со штрихкодом")
    result = packing.scan(account, user, barcode_of(stranger["sku"]))
    assert result["action"] == "wrong_product"
    assert "СТОП" in result["message"]


def test_set_reaches_the_shipment_report_once(account, set_posting, user):
    """В отчёт уходит набор, а не его содержимое."""
    sku, parts = set_posting["sku"], set_posting["parts"]
    state = packing.load_state(account, user)
    posting_number = state["active"]["posting_number"]
    item = next(i for i in state["items"] if i["sku"] == sku)
    for _ in range(item["need"]):
        for part in parts:
            packing.scan(account, user, barcode_of(part["sku"]))

    rows = db.query(
        "SELECT sku, status FROM shipped_items WHERE account_id = ? AND posting_number = ?",
        (account["id"], posting_number),
    )
    shipped = [row["sku"] for row in rows if row["status"] == "ok"]
    assert shipped.count(sku) == item["need"], "набор зачтён не столько раз, сколько нужно"
    assert not any(row["sku"] in {p["sku"] for p in parts} for row in rows), \
        "в отчёт попало содержимое набора вместо самого набора"


def test_set_own_barcode_still_works(account, set_posting, user):
    """Есть наклейка набора — сканируем её, части не нужны."""
    sku = set_posting["sku"]
    packing.scan(account, user, barcode_of(sku))
    item = next(i for i in packing.load_state(account, user)["items"] if i["sku"] == sku)
    assert item["scanned"] >= 1
    # Сканировать части после этого уже не надо: их нужное количество упало.
    assert all(part["need"] == 0 for part in item["parts"]) or item["ok"]


def test_inactive_set_packs_as_an_ordinary_product(account, set_posting, user):
    """Выключенный набор — обычный товар, части в нём не считаются."""
    sku, parts = set_posting["sku"], set_posting["parts"]
    db.execute("UPDATE product_sets SET active = 0 WHERE account_id = ? AND sku = ?",
               (account["id"], sku))
    item = next(i for i in packing.load_state(account, user)["items"] if i["sku"] == sku)
    assert not item.get("is_set")
    result = packing.scan(account, user, barcode_of(parts[0]["sku"]))
    assert result["action"] == "wrong_product"


# ------------------------------------------------------------------ раздел
@pytest.fixture
def client(sample_data):
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def login(client, login="admin", password="test-admin-pass") -> str:
    client.post("/login", data={"login": login, "password": password, "next": "/products"})
    page = client.get("/products")
    return re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1)


def test_products_page_is_admin_only(client):
    from app.core.security import hash_password

    db.execute(
        "INSERT INTO users(login, password_hash, role, active, created_at) VALUES(?,?,?,1,?)",
        ("packer7", hash_password("packer123456"), "packer", db.now_iso()),
    )
    client.post("/login", data={"login": "packer7", "password": "packer123456"})
    assert client.get("/products").status_code == 403
    assert client.get("/api/products/search?q=ко").status_code == 403
    assert "/products" not in client.get("/pack").text


def test_page_lists_the_catalog(client):
    login(client)
    page = client.get("/products")
    assert page.status_code == 200
    row = db.query_one("SELECT name FROM products WHERE account_id = ? LIMIT 1", (account_id(),))
    assert row["name"] in page.text


def test_search_finds_by_barcode(client):
    login(client)
    row = db.query_one("SELECT sku, barcode FROM product_barcodes WHERE account_id = ? LIMIT 1",
                       (account_id(),))
    found = client.get(f"/api/products/search?q={row['barcode']}").json()
    assert [item["sku"] for item in found["items"]] == [row["sku"]]


def test_set_is_created_through_the_api(client):
    csrf = login(client)
    sku = pick_posting(positions=1)["items"][0]["sku"]
    part = db.query_one(
        "SELECT sku FROM products WHERE account_id = ? AND sku != ? LIMIT 1", (account_id(), sku)
    )["sku"]

    response = client.post(
        "/api/products/sets",
        json={"sku": sku, "title": "Подарочный набор",
              "parts": [{"sku": part, "quantity": 2}, {"barcode": "9990000000062", "title": "Открытка"}]},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["set"]["parts"]) == 2

    page = client.get("/products?tab=sets")
    assert "Подарочный набор" in page.text and "9990000000062" in page.text


def test_broken_set_is_refused_with_a_reason(client):
    csrf = login(client)
    sku = pick_posting(positions=1)["items"][0]["sku"]
    response = client.post("/api/products/sets", json={"sku": sku, "parts": []},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 400
    assert "хотя бы одна часть" in response.json()["detail"]


def test_set_api_requires_csrf(client):
    login(client)
    sku = pick_posting(positions=1)["items"][0]["sku"]
    assert client.post("/api/products/sets",
                       json={"sku": sku, "parts": [{"barcode": "1"}]}).status_code == 403


def test_set_is_deleted_through_the_api(client):
    csrf = login(client)
    sku = pick_posting(positions=1)["items"][0]["sku"]
    product_sets.save(account_id(), sku, [{"barcode": "9990000000079"}], user={"login": "admin"})

    # Адрес набора — пара «кабинет и SKU»: раздел общий, и один SKU может
    # встретиться в двух кабинетах.
    where = f"/api/products/sets/{account_id()}/{sku}"
    response = client.request("DELETE", where, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    assert product_sets.get(account_id(), sku) is None
    assert client.request("DELETE", where, headers={"X-CSRF-Token": csrf}).status_code == 404


def test_sets_do_not_leak_between_cabinets(account, sample_data):
    """Кабинеты не пересекаются: набор одного не должен влиять на другой."""
    from app.core import accounts

    sku = pick_posting(positions=1)["items"][0]["sku"]
    make_set(account, sku, [{"barcode": "9990000000086"}])

    second = accounts.get(accounts.create("ozon", "Второй Ozon", "test-client", "test-key"))
    ozon_sync.sync_postings(second)
    ozon_sync.sync_products(second)
    assert product_sets.set_skus(second["id"]) == set()
    assert product_sets.parents_of(second["id"], barcodes=["9990000000086"]) == []


def test_state_survives_a_composition_change(account, set_posting, user):
    """Состав поменяли посреди сборки — панель не должна упасть."""
    sku, parts = set_posting["sku"], set_posting["parts"]
    packing.scan(account, user, barcode_of(parts[0]["sku"]))

    make_set(account, sku, [{"sku": parts[1]["sku"], "quantity": 1}])
    state = packing.load_state(account, user)
    item = next(i for i in state["items"] if i["sku"] == sku)
    assert item["is_set"] is True
    assert [p["sku"] for p in item["parts"]] == [parts[1]["sku"]]
    # Прогресс по выброшенной части просто не учитывается, состояние целое.
    assert item["scanned"] == 0
    assert isinstance(ozon_store.posting_view(db.query_one(
        "SELECT * FROM postings WHERE account_id = ? AND posting_number = ?",
        (account["id"], state["active"]["posting_number"]))), dict)


# ------------------------------------------------- что сказать сборщику
def test_incomplete_label_scan_names_the_missing_parts(account, set_posting, user):
    """«Осталось: набор» никуда не ведёт — нужны названия частей.

    Название набора сборщику не поможет: к полке с ним не пойдёшь, а что внутри
    знает только панель.
    """
    parts = set_posting["parts"]
    packing.scan(account, user, barcode_of(parts[0]["sku"]))
    posting_number = packing.load_state(account, user)["active"]["posting_number"]

    result = packing.scan(account, user, posting_number)
    assert result["action"] == "incomplete", result["message"]
    assert parts[1]["name"] in result["message"], "не названа недостающая часть"
    # Набор назван как заголовок, но не вместо состава.
    assert "набор" in result["message"]
    assert f"{parts[1]['name']} — 1 шт" in result["message"]


def test_missing_list_keeps_plain_products_as_they_were(account, sample_data, user):
    """Обычный товар по-прежнему называется сам, без выдуманных частей."""
    posting = pick_posting(positions=2)
    packing.select_posting(account, user, posting["posting_number"])
    state = packing.load_state(account, user)
    missing = packing.missing_items(state)
    assert len(missing) == len(state["items"])
    for item in state["items"]:
        assert any(item["name"] in line for line in missing)


def test_manual_completion_also_names_the_parts(account, set_posting, user):
    """Кнопка «Завершить без скана стикера» отвечала общей фразой без состава."""
    from fastapi.testclient import TestClient as _TestClient

    parts = set_posting["parts"]
    packing.scan(account, user, barcode_of(parts[0]["sku"]))
    with _TestClient(app, follow_redirects=False) as http:
        http.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/pack"})
        csrf = re.search(r'name="csrf-token" content="([^"]*)"', http.get("/pack").text).group(1)
        # Администратору панель завершить разрешает, поэтому проверяем сборщиком.
        db.execute("UPDATE users SET role = 'packer' WHERE login = 'admin'")
        response = http.post("/api/complete", json={}, headers={"X-CSRF-Token": csrf})
    db.execute("UPDATE users SET role = 'admin' WHERE login = 'admin'")
    assert response.status_code == 400
    assert parts[1]["name"] in response.json()["detail"], response.json()["detail"]
