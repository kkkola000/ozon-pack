"""Ярлыки Маркета — методом generateOrderLabels, по заказу.

GET v2/campaigns/{campaignId}/orders/{orderId}/delivery/labels отдаёт PDF сразу:
ярлыки на все коробки одного заказа. Панель берёт им и печать, и архив до
смены; прежний массовый путь через отчёты больше не используется.
"""
import io
import re
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.core import db
from app.main import app
from app.markets.yandex import client as yandex
from app.markets.yandex import sync as yandex_sync
from app.markets.yandex.client import YandexClient, YandexError


# ------------------------------------------------------------------ запрос к Маркету
def real_client(handler) -> YandexClient:
    """Настоящий клиент, только сеть подменена: проверяем сам запрос."""
    client = YandexClient("9000001", "secret-token", "https://api.partner.market.yandex.ru", max_retries=1)
    client._client = httpx.Client(base_url=client.base_url, headers=client._client.headers,
                                  transport=httpx.MockTransport(handler))
    return client


def test_request_follows_the_documented_method():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(method=request.method, path=request.url.path, query=dict(request.url.params),
                    accept=request.headers.get("accept"), key=request.headers.get("api-key"))
        return httpx.Response(200, content=b"%PDF-1.7 labels", headers={"content-type": "application/pdf"})

    pdf = real_client(handler).order_labels("21000000", "80000001", fmt="A9_HORIZONTALLY")
    assert pdf.startswith(b"%PDF")
    assert seen == {
        "method": "GET",
        "path": "/v2/campaigns/21000000/orders/80000001/delivery/labels",
        "query": {"format": "A9_HORIZONTALLY"},
        "accept": "application/pdf, application/json",
        "key": "secret-token",
    }


def test_market_error_comes_back_with_its_message():
    def handler(_request):
        return httpx.Response(400, json={"status": "ERROR",
                                         "errors": [{"code": "BAD_REQUEST", "message": "Заказ ещё не подтверждён"}]})

    with pytest.raises(YandexError) as caught:
        real_client(handler).order_labels(21000000, 80000001, fmt="A7")
    assert caught.value.message == "Заказ ещё не подтверждён"
    assert caught.value.status == 400 and caught.value.code == "BAD_REQUEST"


def test_not_a_pdf_is_an_error():
    """Пустой или HTML-ответ под видом ярлыка сборщик наклеил бы пустым листом."""
    with pytest.raises(YandexError, match="не PDF"):
        real_client(lambda _r: httpx.Response(200, content=b"<html>")).order_labels(1, 2, fmt="A7")


@pytest.mark.parametrize("campaign, order, fmt", [("", "80000001", "A7"), ("abc", "1", "A7"),
                                                  ("1", "0", "A7"), ("1", "2", "A5")])
def test_bad_input_never_reaches_the_market(campaign, order, fmt):
    def handler(_request):
        raise AssertionError("запрос не должен был уйти")

    with pytest.raises(YandexError):
        real_client(handler).order_labels(campaign, order, fmt=fmt)


def test_all_documented_formats_are_accepted():
    assert yandex.PAGE_FORMATS == ("A9_HORIZONTALLY", "A9", "A7", "A4")
    assert set(yandex.LABEL_FORMATS.values()) <= set(yandex.PAGE_FORMATS)


# ------------------------------------------------------------------ панель
@pytest.fixture
def market(yandex_account):
    yandex_sync.sync_yandex(yandex_account)
    return yandex_account


@pytest.fixture
def client(market):
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/pack"})
        page = test_client.get("/pack")
        test_client.headers["X-CSRF-Token"] = re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1)
        yield test_client


def work_ids(account) -> list[str]:
    return [row["id"] for row in db.query(
        "SELECT id FROM yandex_orders WHERE account_id = ? ORDER BY id", (account["id"],))]


def pages(pdf: bytes) -> int:
    return len(PdfReader(io.BytesIO(pdf)).pages)


def test_archive_has_a_file_per_order_with_all_its_boxes(client, market):
    fake = yandex.get_client(market)
    ids = work_ids(market)
    fake.boxes[ids[0]] = 3          # заказ в три коробки
    response = client.post(f"/api/pack/labels.zip?shop={market['id']}")
    assert response.status_code == 200, response.text

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert sorted(archive.namelist()) == sorted(f"{order_id}.pdf" for order_id in ids)
    assert pages(archive.read(f"{ids[0]}.pdf")) == 3, "ярлыки всех коробок — в файле своего заказа"
    assert all(pages(archive.read(f"{order_id}.pdf")) == 1 for order_id in ids[1:])
    # Каждый заказ — своим запросом, с магазином из самого заказа и форматом по «Принтерам».
    assert sorted(order for _campaign, order, _fmt in fake.label_requests) == sorted(ids)
    stored = {row["campaign_id"] for row in db.query(
        "SELECT campaign_id FROM yandex_orders WHERE account_id = ?", (market["id"],))}
    assert {campaign for campaign, _order, _fmt in fake.label_requests} == stored
    assert {fmt for _campaign, _order, fmt in fake.label_requests} == {"A7"}


def test_one_refused_order_stays_locked_the_rest_unload(client, market, monkeypatch):
    fake = yandex.get_client(market)
    ids = work_ids(market)
    original = fake.order_labels

    def picky(campaign_id, order_id, *, fmt=None):
        if str(order_id) == ids[0]:
            raise YandexError("Заказ ещё не подтверждён", status=400)
        return original(campaign_id, order_id, fmt=fmt)

    monkeypatch.setattr(fake, "order_labels", picky)
    response = client.post(f"/api/pack/labels.zip?shop={market['id']}")
    assert response.status_code == 200, response.text
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert f"{ids[0]}.pdf" not in names and len(names) == len(ids) - 1
    saved = db.query_one("SELECT label_saved_at FROM yandex_orders WHERE id = ?", (ids[0],))
    assert saved["label_saved_at"] is None, "неотданный ярлык ждёт следующей выгрузки"


def test_printing_several_orders_is_one_pdf_in_the_asked_order(client, market):
    fake = yandex.get_client(market)
    ids = work_ids(market)[:3]
    fake.boxes[ids[1]] = 2
    response = client.post("/api/orders/labels.pdf", json={"account_id": market["id"], "ids": ids})
    assert response.status_code == 200, response.text
    assert pages(response.content) == 4
    assert sorted(order for _c, order, _f in fake.label_requests) == sorted(ids)
    printed = db.query(f"SELECT print_count FROM yandex_orders WHERE id IN ({','.join('?' * 3)})", ids)
    assert all(row["print_count"] == 1 for row in printed)


def test_half_a_print_is_refused_and_not_marked(client, market, monkeypatch):
    """Напечатать половину нельзя: сборщик не заметит, что одного ярлыка нет."""
    fake = yandex.get_client(market)
    ids = work_ids(market)[:2]
    original = fake.order_labels

    def picky(campaign_id, order_id, *, fmt=None):
        if str(order_id) == ids[1]:
            raise YandexError("Заказ отменён", status=400)
        return original(campaign_id, order_id, fmt=fmt)

    monkeypatch.setattr(fake, "order_labels", picky)
    response = client.post("/api/orders/labels.pdf", json={"account_id": market["id"], "ids": ids})
    assert response.status_code == 502
    assert ids[1] in response.json()["detail"] and "Заказ отменён" in response.json()["detail"]
    marked = db.query_one("SELECT print_count FROM yandex_orders WHERE id = ?", (ids[0],))
    assert marked["print_count"] == 0


def test_order_without_campaign_asks_to_refresh(client, market):
    order_id = work_ids(market)[0]
    db.execute("UPDATE yandex_orders SET campaign_id = NULL WHERE id = ?", (order_id,))
    response = client.post("/api/orders/labels.pdf", json={"account_id": market["id"], "ids": [order_id]})
    assert response.status_code == 502
    assert "обновите заказы" in response.json()["detail"]
