"""«Лист с заказами» на «Сборке»: что взять с полки под заказы к сборке.

Кнопка в блоке «Очередь», под «Обновить заказы». Лист — PDF: кабинет, фото,
название товара, артикул и количество. Заказы — «К сборке» под фильтром
кабинетов; одинаковый товар кабинета — одной строкой с общим количеством.

Фото скачивает сервер, поэтому отдельно проверяется, что по ссылке он ходит
не куда угодно: адреса самого сервера и внутренней сети отсекаются, и
переадресация туда тоже.
"""
import base64
import io
import re

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader

from app.core import accounts, board as core_board, db, orders_sheet, packing, photos, sync
from app.main import app
from app.markets.avito import sync as avito_sync


def png(color=(200, 60, 40), size=(300, 200)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def data_url(raw: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def text_of(pdf: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)


@pytest.fixture(autouse=True)
def fresh_cache():
    photos._cache.clear()
    yield
    photos._cache.clear()


@pytest.fixture
def cabinets(sample_data):
    avito_one = accounts.get(accounts.create("avito", "Магазин Avito", "test-client", "test-secret"))
    avito_sync.sync_avito(avito_one)
    return {"ozon": accounts.default_account(), "avito": avito_one}


@pytest.fixture
def client(cabinets):
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.post("/login", data={"login": "admin", "password": "test-admin-pass"})
        page = test_client.get("/pack")
        test_client.headers["X-CSRF-Token"] = re.search(
            r'name="csrf-token" content="([^"]*)"', page.text
        ).group(1)
        yield test_client


# ------------------------------------------------------------------ строки листа
def test_sheet_has_every_item_of_orders_to_pack(cabinets):
    where = packing.shops(packing.ALL)
    sheet = orders_sheet.collect(where)
    cards = core_board.rows(where, "deliver")
    assert sheet["orders"] == len(cards) > 0
    assert sheet["pieces"] == sum(int(item["quantity"]) for card in cards for item in card["items"])
    assert {row["shop"] for row in sheet["rows"]} == {"Ozon", "Магазин Avito"}
    for row in sheet["rows"]:
        assert row["name"] and row["quantity"] > 0


def test_same_product_of_a_cabinet_is_one_row(cabinets):
    sheet = orders_sheet.collect([cabinets["ozon"]])
    keys = [(row["account_id"], row["code"]) for row in sheet["rows"]]
    assert len(keys) == len(set(keys)), "одинаковый товар кабинета — одной строкой"
    needed = db.query(
        "SELECT i.offer_id, SUM(i.quantity) AS q FROM posting_items i "
        "JOIN postings p ON p.account_id = i.account_id AND p.posting_number = i.posting_number "
        "WHERE p.account_id = ? AND p.status = 'awaiting_deliver' AND p.local_state = 'new' GROUP BY i.offer_id",
        (cabinets["ozon"]["id"],),
    )
    assert {row["code"]: row["quantity"] for row in sheet["rows"]} == {r["offer_id"]: r["q"] for r in needed}
    assert any(row["orders"] > 1 for row in sheet["rows"]), "в подделке товар встречается в двух заказах"


def test_packed_and_waiting_orders_are_not_in_the_sheet(cabinets, user):
    from app.markets.ozon import pack as ozon_pack
    from tests.conftest import pick_posting

    before = orders_sheet.collect([cabinets["ozon"]])["pieces"]
    posting = pick_posting()
    ozon_pack.complete(cabinets["ozon"], user, posting["posting_number"])
    after = orders_sheet.collect([cabinets["ozon"]])["pieces"]
    assert after == before - sum(int(item["quantity"]) for item in posting["items"])


def test_filter_narrows_the_sheet_to_one_cabinet(cabinets):
    sheet = orders_sheet.collect(packing.shops(str(cabinets["avito"]["id"])))
    assert sheet["rows"] and {row["shop"] for row in sheet["rows"]} == {"Магазин Avito"}


# ------------------------------------------------------------------ PDF
def test_pdf_has_columns_names_articles_and_photos(cabinets):
    sku = db.query_one("SELECT i.sku FROM posting_items i JOIN postings p ON p.account_id = i.account_id "
                       "AND p.posting_number = i.posting_number WHERE p.status = 'awaiting_deliver' LIMIT 1")["sku"]
    db.execute("UPDATE products SET image = ? WHERE sku = ?", (data_url(png()), sku))
    sheet = orders_sheet.collect(packing.shops(packing.ALL))
    pdf = orders_sheet.build(sheet, user={"login": "admin"}, scope="все кабинеты")

    text = text_of(pdf)
    for word in ("Лист с заказами", "Кабинет", "Фото", "Название товара", "Артикул", "Кол-во",
                 "Магазин Avito", f"Итого: {sheet['pieces']} шт."):
        assert word in text, word
    for row in sheet["rows"][:5]:
        assert row["name"][:15] in text
        assert row["code"] in text
    assert "нет фото" in text, "у товаров без фото — подпись, а не пустая клетка"
    images = sum(len(page.images) for page in PdfReader(io.BytesIO(pdf)).pages)
    assert images >= 1, "фото товара должно быть на листе"


def test_empty_sheet_is_refused(sample_data):
    db.execute("UPDATE postings SET local_state = 'packed'")
    with pytest.raises(orders_sheet.NothingToPick):
        orders_sheet.build(orders_sheet.collect(packing.shops(packing.ALL)), user={"login": "a"}, scope="все")


# ------------------------------------------------------------------ фото
@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/a.jpg", "http://localhost/a.jpg", "http://127.0.0.1/a.jpg",
    "http://10.0.0.5/a.jpg", "http://192.168.1.2/a.jpg", "http://[::1]/a.jpg", "http://169.254.169.254/x",
    "https://printer.local/a.jpg", "", "not a url",
])
def test_photo_links_into_the_server_are_refused(url):
    assert photos.allowed(url) is False


def test_photo_link_to_the_internet_is_allowed():
    assert photos.allowed("https://93.184.216.34/photo.jpg")
    assert photos.allowed("https://cdn.example/photo.jpg")


def test_thumbnail_is_a_square_jpeg():
    thumb = photos.thumbnail(png(size=(400, 100)))
    image = Image.open(io.BytesIO(thumb))
    assert image.format == "JPEG" and image.size == (photos.SIDE, photos.SIDE)
    assert photos.thumbnail(b"not an image") is None


def test_broken_photo_does_not_break_the_sheet(monkeypatch):
    calls = []

    def fake_download(url):
        calls.append(url)
        return png() if url.endswith("good.jpg") else b"broken"

    monkeypatch.setattr(photos, "_download", fake_download)
    found = photos.thumbnails(["https://cdn.example/good.jpg", "https://cdn.example/bad.jpg",
                               "https://cdn.example/good.jpg", None, ""])
    assert list(found) == ["https://cdn.example/good.jpg"]
    assert len(calls) == 2, "одинаковая ссылка качается один раз"
    photos.thumbnails(["https://cdn.example/good.jpg", "https://cdn.example/bad.jpg"])
    assert len(calls) == 2, "скачанное помнится"


def test_redirect_into_the_server_is_not_followed(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "cdn.example":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8080/api/secret"})
        return httpx.Response(200, content=png(), headers={"content-type": "image/png"})

    real = httpx.Client
    monkeypatch.setattr(photos.httpx, "Client",
                        lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    assert photos._download("https://cdn.example/a.jpg") is None
    assert seen == ["https://cdn.example/a.jpg"], "по переадресации во внутреннюю сеть не ходим"


def test_photo_is_downloaded_with_a_size_limit(monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b"x" * 64, headers={"content-type": "image/png"})

    real = httpx.Client
    monkeypatch.setattr(photos.httpx, "Client",
                        lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(photos, "MAX_BYTES", 16)
    assert photos._download("https://cdn.example/big.jpg") is None


# ------------------------------------------------------------------ кнопка
def test_button_is_under_refresh_in_the_queue(client):
    page = client.get("/pack").text
    queue = page[page.index('id="idle-panel"'):page.index("Последние сканы")]
    assert queue.index('id="btn-sync"') < queue.index('id="btn-sheet"')
    assert "Лист с заказами" in queue


def test_sheet_is_downloaded_as_pdf(client, cabinets):
    response = client.post(f"/api/pack/orders-sheet.pdf?shop={cabinets['ozon']['id']}")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert re.search(r'attachment; filename="list-zakazov-kabinet-\d+-[\d_-]+\.pdf"',
                     response.headers["content-disposition"])
    text = text_of(response.content)
    assert "Лист с заказами · Ozon" in text
    assert "Магазин Avito" not in text, "фильтр кабинета сужает лист"
    assert db.query_one("SELECT 1 FROM events WHERE kind = 'orders_sheet'")


def test_sheet_needs_csrf(client):
    response = client.post("/api/pack/orders-sheet.pdf", headers={"X-CSRF-Token": "wrong-token"})
    assert response.status_code == 403


def test_no_orders_answers_with_a_message(client):
    db.execute("UPDATE postings SET local_state = 'packed'")
    db.execute("UPDATE avito_orders SET local_state = 'packed'")
    response = client.post("/api/pack/orders-sheet.pdf")
    assert response.status_code == 400
    assert "Заказов к сборке нет" in response.json()["detail"]


def test_second_ozon_cabinet_is_its_own_rows(sample_data):
    second = accounts.get(accounts.create("ozon", "Второй склад", "test-client", "test-key"))
    sync.sync_all()
    sheet = orders_sheet.collect(packing.shops(packing.ALL))
    shops = [row["shop"] for row in sheet["rows"]]
    assert "Второй склад" in shops and "Ozon" in shops
    # Кабинеты не перемешаны: сначала весь первый, потом второй — как в «Настройках».
    assert shops.index("Второй склад") > max(i for i, shop in enumerate(shops) if shop == "Ozon")
    assert second
