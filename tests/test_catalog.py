"""Загрузка каталога кабинета целиком.

Обычная синхронизация тянет карточки только тех товаров, что встретились в
заказах и возвратах. Для сборки этого хватает, а для наборов нет: набор
собирают из складских остатков, и в списке выбора должен быть весь каталог.

Архив при этом не нужен: архивный товар не продаётся, и в выборе он только
мешает — у живого склада его бывает больше, чем живых позиций.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app import catalog, db, ozon, product_sets
from app.main import app
from app.ozon import OzonError
from tests.conftest import account_id
from tests.fakes import CATALOG_ARCHIVED, CATALOG_EXTRA


def live_skus():
    return {row["sku"] for row in db.query(
        "SELECT sku FROM products WHERE account_id = ? AND archived = 0", (account_id(),))}


def all_skus():
    return {row["sku"] for row in db.query(
        "SELECT sku FROM products WHERE account_id = ?", (account_id(),))}


# ------------------------------------------------------------------ обход
def test_ordinary_sync_does_not_bring_the_whole_catalog(account, sample_data):
    """Отсюда и жалоба «загружается не весь список»."""
    catalog_only = {sku for sku, _o, _n, _b in CATALOG_EXTRA}
    assert catalog_only & all_skus() == set(), \
        "товары не из заказов появились в каталоге сами — проверка больше ничего не ловит"


def test_refresh_loads_the_whole_catalog(account, sample_data):
    result = catalog.refresh(account)
    for sku, _offer, _name, _bc in CATALOG_EXTRA:
        assert sku in live_skus(), "товар каталога не загрузился"
    assert result["live"] == len(live_skus())
    assert result["offers"] >= len(CATALOG_EXTRA)


def test_archived_products_do_not_become_catalog(account, sample_data):
    """Архив приходит в том же ответе Ozon — панель обязана его отсеять."""
    catalog.refresh(account)
    for sku, _offer, _name, _bc in CATALOG_ARCHIVED:
        assert sku not in live_skus(), "архивный товар попал в каталог"


def test_archive_is_counted_not_hidden_silently(account, sample_data):
    result = catalog.refresh(account)
    assert result["archived_skipped"] == len(CATALOG_ARCHIVED)


def test_product_that_left_the_catalog_goes_to_archive(account, sample_data):
    """Товар убрали с площадки — из раздела он уходит, из базы нет.

    Удалять нельзя: по его штрихкоду могут собирать заказ, который уже в работе.
    """
    catalog.refresh(account)
    gone = sorted(live_skus())[0]
    barcode = db.query_one(
        "SELECT barcode FROM product_barcodes WHERE account_id = ? AND sku = ?",
        (account_id(), gone),
    )
    client = ozon.get_client(account)
    original = client.product_list

    def without_it(*, limit=1000, last_id=""):
        items, tail, total = original(limit=limit, last_id=last_id)
        return [i for i in items if str(i["product_id"]) != gone], tail, total

    client.product_list = without_it
    catalog.refresh(account)
    client.product_list = original

    assert gone not in live_skus(), "исчезнувший товар остался в каталоге"
    assert gone in all_skus(), "строка товара удалена — штрихкод перестанет сканироваться"
    if barcode:
        assert db.query_one(
            "SELECT sku FROM product_barcodes WHERE account_id = ? AND barcode = ?",
            (account_id(), barcode["barcode"]),
        ) is not None


def test_product_back_from_archive_is_live_again(account, sample_data):
    catalog.refresh(account)
    sku = sorted(live_skus())[0]
    db.execute("UPDATE products SET archived = 1 WHERE account_id = ? AND sku = ?",
               (account_id(), sku))
    catalog.refresh(account)
    assert sku in live_skus(), "товар вернулся в продажу, а панель его прячет"


def test_unknown_archive_flag_keeps_the_product(account, sample_data):
    """Спрятать живой товар хуже, чем показать архивный: набор не соберёшь."""
    assert catalog._is_archived({"offer_id": "X"}) is False
    assert catalog._is_archived({"archived": None}) is False
    assert catalog._is_archived({"archived": True}) is True
    assert catalog._is_archived({"is_archived": "true"}) is True


def test_refresh_survives_a_second_run(account, sample_data):
    """Повтор не плодит дублей и не отправляет всё в архив."""
    first = catalog.refresh(account)
    second = catalog.refresh(account)
    assert second["live"] == first["live"]
    assert second["archived_marked"] == 0


def test_catalog_walk_stops_on_the_last_page(account, sample_data):
    """Курсор отдал пусто — обход заканчивается, а не крутится до предела."""
    client = ozon.get_client(account)
    calls = []
    original = client.product_list

    def counted(*, limit=1000, last_id=""):
        calls.append(last_id)
        return original(limit=limit, last_id=last_id)

    client.product_list = counted
    catalog.offers_of(client)
    client.product_list = original
    assert len(calls) < catalog.MAX_PAGES


# ------------------------------------------------------------------ раздел
@pytest.fixture
def client(sample_data):
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def login(client) -> str:
    client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/products"})
    return re.search(r'name="csrf-token" content="([^"]*)"', client.get("/products").text).group(1)


def test_archived_products_are_not_offered_for_sets(account, client):
    csrf = login(client)
    catalog.refresh(account)
    found = client.get("/api/products/search?q=архив").json()
    assert found["items"] == [], "архивный товар предлагается в набор"
    assert csrf


def test_archived_products_are_not_in_the_catalog_page(account, client):
    login(client)
    catalog.refresh(account)
    page = client.get("/products")
    for _sku, _offer, name, _bc in CATALOG_ARCHIVED:
        assert name not in page.text, "архивный товар показан в разделе"
    for _sku, _offer, name, _bc in CATALOG_EXTRA:
        assert name in page.text, "товар каталога не показан в разделе"


def test_page_says_how_much_archive_was_skipped(account, client):
    """Молча отсеивать нельзя: иначе «а где мой товар?» не на что ответить."""
    login(client)
    catalog._save_job(account["id"], status="ok", live=11, archived_skipped=2)
    page = client.get("/products").text
    assert "архив пропущен" in page and "2" in page


def test_refresh_button_starts_the_job(account, client, monkeypatch):
    csrf = login(client)
    done = {}

    def fake_start(acc, user=None):
        done["account"] = acc["id"]
        done["by"] = (user or {}).get("login")
        return {"status": "started", "message": "Обновляем каталог"}

    monkeypatch.setattr(catalog, "start", fake_start)
    response = client.post("/api/products/catalog/refresh", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    assert done == {"account": account["id"], "by": "admin"}


def test_refresh_requires_csrf(client):
    login(client)
    assert client.post("/api/products/catalog/refresh").status_code == 403


def test_refresh_is_admin_only(client):
    from app.security import hash_password

    db.execute(
        "INSERT INTO users(login, password_hash, role, active, created_at) VALUES(?,?,?,1,?)",
        ("packer5", hash_password("packer123456"), "packer", db.now_iso()),
    )
    client.post("/login", data={"login": "packer5", "password": "packer123456"})
    assert client.post("/api/products/catalog/refresh").status_code == 403
    assert client.get("/api/products/catalog/status").status_code == 403


def test_status_reports_the_outcome(account, client):
    login(client)
    catalog._save_job(account["id"], status="ok", live=42, archived_skipped=7)
    data = client.get("/api/products/catalog/status").json()
    assert data["status"] == "ok" and data["live"] == 42
    assert data["running"] is False


def test_failed_refresh_is_reported_not_swallowed(account, client, monkeypatch):
    """Ozon отказал — человек должен это увидеть, а не гадать."""
    login(client)

    def broken(_client, **kwargs):
        raise OzonError("Ozon отказал", status=403)

    monkeypatch.setattr(catalog, "offers_of", broken)
    with pytest.raises(OzonError):
        catalog.refresh(account)


def test_second_start_does_not_run_twice(account, monkeypatch):
    """Два обхода одного кабинета пишут в одни строки — один лишний."""
    catalog._running.add(account["id"])
    try:
        assert catalog.start(account, {"login": "admin"})["status"] == "running"
    finally:
        catalog._running.discard(account["id"])


# ------------------------------------------------------------------ сборка
def test_archived_product_still_scans_in_an_open_order(account, sample_data, user):
    """Штрихкод архивного товара не должен ломать начатую сборку."""
    from app import packing
    from tests.conftest import barcode_of, pick_posting

    posting = pick_posting(positions=1)
    sku = posting["items"][0]["sku"]
    catalog.refresh(account)
    db.execute("UPDATE products SET archived = 1 WHERE account_id = ? AND sku = ?",
               (account_id(), sku))

    packing.select_posting(account, user, posting["posting_number"])
    result = packing.scan(account, user, barcode_of(sku))
    assert result["status"] == "ok", result["message"]


def test_archived_part_of_a_set_keeps_working(account, sample_data):
    """Часть набора ушла в архив — состав не рассыпается."""
    sku = next(iter(sorted(live_skus() or {"1234567890"})))
    catalog.refresh(account)
    part = db.query_one(
        "SELECT sku FROM products WHERE account_id = ? AND sku != ? AND archived = 0 LIMIT 1",
        (account_id(), sku),
    )["sku"]
    product_sets.save(account_id(), sku, [{"sku": part, "quantity": 1}], user={"login": "admin"})
    db.execute("UPDATE products SET archived = 1 WHERE account_id = ? AND sku = ?",
               (account_id(), part))

    saved = product_sets.get(account_id(), sku)
    assert [p["part_sku"] for p in saved["parts"]] == [part]
    assert product_sets.parents_of(account_id(), sku=part), "часть перестала опознаваться"


def test_empty_walk_does_not_wipe_the_catalog(account, sample_data):
    """Ozon ответил пусто — это похоже на сбой, а не на опустевший склад.

    Отправить весь каталог в архив на таком основании нельзя: раздел опустеет,
    и наборы будет не из чего собирать.
    """
    catalog.refresh(account)
    before = live_skus()
    assert before

    client = ozon.get_client(account)
    original = client.product_list
    client.product_list = lambda *, limit=1000, last_id="": ([], "", 0)
    result = catalog.refresh(account)
    client.product_list = original

    assert result["archived_marked"] == 0
    assert live_skus() == before, "пустой ответ отправил каталог в архив"
