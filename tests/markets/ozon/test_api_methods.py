"""Методы Ozon Seller API по актуальной документации.

* /v4/posting/fbs/list — статусы списком, страницы по курсору;
* стикеры — только заданием: /v3/posting/fbs/package-label/create и
  /v2/posting/fbs/package-label/get (синхронный /v2/posting/fbs/package-label
  Ozon отключает 2 ноября 2026 года);
* /v3/product/list — число товаров в result.total_items (result.total
  отключат 23 ноября 2026 года).

Здесь настоящий клиент, подменена только сеть: проверяется сам запрос и
разбор ответа, а не подделка.
"""
import io
import json
import re
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import accounts, db
from app.main import app
from app.markets.ozon import client as ozon
from app.markets.ozon import store
from app.markets.ozon import sync as ozon_sync
from app.markets.ozon.client import OzonClient, OzonError
from tests.pdfstub import make_label_pdf

BASE = "https://api-seller.ozon.ru"


def real_client(handler) -> OzonClient:
    client = OzonClient(client_id="123", api_key="secret", base_url=BASE, max_retries=1)
    client._client = httpx.Client(base_url=BASE, headers=client._client.headers,
                                  transport=httpx.MockTransport(handler))
    return client


def label_pdf(*numbers) -> bytes:
    return make_label_pdf([{"posting_number": n, "order_number": "", "city": "", "warehouse": "",
                            "tpl": "", "products": []} for n in numbers])


# ------------------------------------------------------------------ /v4/posting/fbs/list
def test_posting_list_asks_v4_with_statuses_and_cursor():
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"postings": [{"posting_number": "1-1-1"}], "cursor": "abc", "has_next": True})

    from datetime import datetime, timezone
    moment = datetime(2026, 9, 26, tzinfo=timezone.utc)
    postings, cursor, has_next = real_client(handler).posting_list(
        ["awaiting_packaging", "awaiting_deliver"], moment, moment, cursor="prev")
    path, body = seen[0]
    assert path == "/v4/posting/fbs/list"
    assert body["sort_dir"] == "asc"
    assert body["filter"]["statuses"] == ["awaiting_packaging", "awaiting_deliver"]
    assert body["filter"]["since"] == "2026-09-26T00:00:00.000Z"
    assert body["cursor"] == "prev" and body["limit"] == ozon.POSTINGS_PAGE_LIMIT
    # Лишнего не просим: финансы и транслит панели не нужны.
    assert body["with"] == {"analytics_data": True, "barcodes": True}
    assert "offset" not in body and "dir" not in body and "status" not in body["filter"]
    assert (postings, cursor, has_next) == ([{"posting_number": "1-1-1"}], "abc", True)


def test_sync_walks_the_cursor_and_takes_both_statuses_at_once(account, monkeypatch):
    fake = ozon.get_client(account)
    original = fake.posting_list
    monkeypatch.setattr(fake, "posting_list",
                        lambda statuses, since, to, *, cursor="", **_kwargs: original(
                            statuses, since, to, limit=5, cursor=cursor))
    result = ozon_sync.sync_postings(account)
    assert result["saved"] == len([p for p in fake._postings.values() if p["status"] in store.WORK_STATUSES])
    statuses = {request[0] for request in fake.list_requests}
    assert statuses == {tuple(store.WORK_STATUSES)}, "оба статуса — одним обходом"
    assert [request[1] for request in fake.list_requests] == ["", "5", "10"]


def test_sync_stops_on_a_cursor_that_does_not_move(account, monkeypatch):
    fake = ozon.get_client(account)
    calls = []

    def stuck(statuses, since, to, *, limit=100, cursor=""):
        calls.append(cursor)
        return [json.loads(json.dumps(next(iter(fake._postings.values()))))], "same", True

    monkeypatch.setattr(fake, "posting_list", stuck)
    ozon_sync.sync_postings(account)
    assert calls == ["", "same"], "курсор не сдвинулся — обход должен закончиться"


V4_POSTING = {
    "posting_number": "0208245774-0029-1", "order_id": 33301885134, "order_number": "0208245774-0029",
    "status": "awaiting_deliver", "substatus": "posting_created",
    "in_process_at": "2026-05-14T08:00:00.000Z", "shipment_date": "2026-05-18T12:00:00.000Z",
    "delivering_date": "2026-05-19T15:30:00.000Z", "tracking_number": "TRK1234567890",
    "is_express": False, "is_multibox": False, "multi_box_qty": 1,
    "addressee": {"name": "Получатель"},
    "customer": {"name": "Покупатель", "address": {"address_tail": "д. 1", "city": "Москва"}, "customer_id": 1},
    "analytics_data": {"city": "Москва", "region": "Московская область", "delivery_type": "PVZ",
                       "is_premium": True, "payment_type_group_name": "Банковская карта",
                       "tpl_provider": "Доставка Ozon", "warehouse": "Софьино", "warehouse_id": 17023},
    "barcodes": {"lower_barcode": "601302481800001", "upper_barcode": "601302481800001"},
    "scanit": "601302481800001-SCANIT",
    "cancellation": {"cancel_reason": ""},
    "delivery_method": {"id": 20605650762000, "name": "Доставка Ozon самостоятельно, Софьино",
                        "tpl_provider": "Доставка Ozon", "warehouse": "17023", "warehouse_id": 20605650762000},
    "products": [
        {"name": "Смартфон", "offer_id": "b4408110", "price": {"amount": "12990.00", "currency": "RUB"},
         "quantity": 1, "sku": 1904686181},
        {"name": "Наушники", "offer_id": "h667722", "price": {"amount": "3490.00", "currency": "RUB"},
         "quantity": 2, "sku": 1904686182},
    ],
    "requirements": {"products_requiring_gtd": ["1904686181"], "products_requiring_mandatory_mark": []},
}


def test_v4_posting_is_parsed(account):
    with db.write() as conn:
        store.upsert_posting(conn, account["id"], json.loads(json.dumps(V4_POSTING)))
    row = db.query_one("SELECT * FROM postings WHERE posting_number = ?", (V4_POSTING["posting_number"],))
    assert row["status"] == "awaiting_deliver" and row["items_count"] == 3
    # В /v4 в delivery_method.warehouse — номер; название склада берётся из аналитики.
    assert row["warehouse_name"] == "Софьино"
    assert row["requires_gtd"] == 1
    # Штрихкод новой этикетки FBS — поле scanit отправления.
    assert row["scanit"] == "601302481800001-SCANIT"
    items = {r["sku"]: dict(r) for r in db.query(
        "SELECT sku, price, currency, quantity FROM posting_items WHERE posting_number = ?",
        (V4_POSTING["posting_number"],))}
    assert items["1904686181"]["price"] == "12990.00" and items["1904686181"]["currency"] == "RUB"
    assert items["1904686182"]["quantity"] == 2
    # Получатель панели не нужен — в сохранённом ответе его нет.
    stored = json.loads(row["raw"])
    assert "customer" not in stored and "addressee" not in stored
    assert stored["analytics_data"]["city"] == "Москва"


def test_v3_posting_format_still_parses(account):
    """Одно отправление (/v3/posting/fbs/get) отдаёт цену строкой — так и остаётся."""
    old = json.loads(json.dumps(V4_POSTING))
    old["posting_number"] = "0208245774-0029-2"
    old["products"] = [{"name": "Смартфон", "offer_id": "b4408110", "price": "12990.00",
                        "currency_code": "RUB", "quantity": 1, "sku": 1904686181}]
    old["delivery_method"]["warehouse"] = "Софьино-склад"
    with db.write() as conn:
        store.upsert_posting(conn, account["id"], old)
    row = db.query_one("SELECT warehouse_name FROM postings WHERE posting_number = ?", (old["posting_number"],))
    assert row["warehouse_name"] == "Софьино-склад"
    item = db.query_one("SELECT price, currency FROM posting_items WHERE posting_number = ?", (old["posting_number"],))
    assert (item["price"], item["currency"]) == ("12990.00", "RUB")


# ------------------------------------------------------------------ стикеры
class LabelServer:
    """Seller API в миниатюре: задание на стикеры, его статус и файл."""

    def __init__(self, get_answers):
        self.requests = []
        self.get_answers = list(get_answers)

    def __call__(self, request):
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        if request.url.path == "/v3/posting/fbs/package-label/create":
            return httpx.Response(200, json={"tasks": [{"task_id": 11, "task_type": "big_label"},
                                                       {"task_id": 12, "task_type": "small_label"}]})
        if request.url.path == "/v2/posting/fbs/package-label/get":
            return httpx.Response(200, json=self.get_answers.pop(0))
        if request.url.path == "/files/labels.pdf":
            return httpx.Response(200, content=label_pdf("1-1-1", "2-2-2"))
        return httpx.Response(404, json={"message": "нет такого метода"})

    def paths(self):
        return [path for _method, path, _body in self.requests]


READY = {"file_url": f"{BASE}/files/labels.pdf", "status": {"code": "completed", "postings_count": 2,
                                                             "printed_postings_count": 2, "unprinted_postings": []}}


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(ozon, "LABEL_POLL", 0)
    monkeypatch.setattr(ozon, "label_size", lambda: None)


def test_labels_go_through_the_task(monkeypatch):
    server = LabelServer([{"status": {"code": "in_progress"}}, READY])
    pdf, name = real_client(server).package_label(["1-1-1", "2-2-2"])
    assert pdf.startswith(b"%PDF") and name == "label.pdf"
    assert server.paths() == ["/v3/posting/fbs/package-label/create", "/v2/posting/fbs/package-label/get",
                              "/v2/posting/fbs/package-label/get", "/files/labels.pdf"]
    assert server.requests[0][2] == {"posting_numbers": ["1-1-1", "2-2-2"]}
    assert server.requests[1][2] == {"task_id": 11}, "обычная этикетка — по умолчанию"
    # Отключаемый метод больше не вызывается.
    assert "/v2/posting/fbs/package-label" not in server.paths()


def test_small_label_is_taken_for_a_58x40_tape(monkeypatch):
    monkeypatch.setattr(ozon, "label_size", lambda: "58x40")
    server = LabelServer([READY])
    real_client(server).package_label(["1-1-1"])
    assert server.requests[1][2] == {"task_id": 12}


def test_unprinted_posting_is_named_for_print_and_left_out_of_the_batch():
    answer = json.loads(json.dumps(READY))
    answer["status"]["unprinted_postings"] = [{"posting_number": "2-2-2", "message": "Отправление отменено"}]
    with pytest.raises(OzonError, match="2-2-2.*Отправление отменено"):
        real_client(LabelServer([answer])).package_label(["1-1-1", "2-2-2"])
    pdf, missing = real_client(LabelServer([answer])).package_label_batch(["1-1-1", "2-2-2"])
    assert pdf.startswith(b"%PDF") and missing == {"2-2-2": "Отправление отменено"}


def test_task_error_is_reported():
    server = LabelServer([{"error": {"code": "INVALID_STATE", "message": "Отправления не в статусе awaiting_deliver"},
                           "status": {"code": "error"}}])
    with pytest.raises(OzonError, match="awaiting_deliver"):
        real_client(server).package_label(["1-1-1"])


def test_file_on_a_foreign_host_is_refused():
    answer = json.loads(json.dumps(READY))
    answer["file_url"] = "https://evil.example/labels.pdf"
    with pytest.raises(OzonError, match="чужому адресу"):
        real_client(LabelServer([answer])).package_label(["1-1-1"])


def test_waiting_has_a_limit(monkeypatch):
    monkeypatch.setattr(ozon, "LABEL_WAIT", 0)
    server = LabelServer([{"status": {"code": "in_progress"}}] * 3)
    with pytest.raises(OzonError, match="Истекло время"):
        real_client(server).label_file(11, ["1-1-1"], wait=0)


# ------------------------------------------------------------------ архив до смены
@pytest.fixture
def client(sample_data):
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/pack"})
        page = test_client.get("/pack")
        test_client.headers["X-CSRF-Token"] = re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1)
        yield test_client


def test_archive_keeps_unprinted_postings_locked(client):
    fake = ozon.get_client(accounts.default_account())
    waiting = [row["posting_number"] for row in db.query(
        "SELECT posting_number FROM postings WHERE status = 'awaiting_deliver' AND label_saved_at IS NULL "
        "ORDER BY posting_number")]
    refused = waiting[0]
    fake.unprinted[refused] = "Стикер ещё формируется"
    response = client.post("/api/pack/labels.zip")
    assert response.status_code == 200, response.text
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert not any(refused in name for name in names)
    saved = db.query_one("SELECT label_saved_at FROM postings WHERE posting_number = ?", (refused,))
    assert saved["label_saved_at"] is None, "без стикера — остаётся в замке"
    others = db.query(f"SELECT label_saved_at FROM postings WHERE posting_number IN ({','.join('?' * (len(waiting) - 1))})",
                      waiting[1:])
    assert all(row["label_saved_at"] for row in others)
    assert db.query_one("SELECT 1 FROM events WHERE kind = 'label_missing' AND posting_number = ?", (refused,))


# ------------------------------------------------------------------ /v3/product/list
def test_product_list_reads_total_items():
    def handler(_request):
        return httpx.Response(200, json={"result": {"items": [{"offer_id": "a"}], "last_id": "x", "total_items": 7}})

    items, last_id, total = real_client(handler).product_list(limit=1)
    assert (len(items), last_id, total) == (1, "x", 7)


def test_product_list_still_reads_total_before_the_switch():
    def handler(_request):
        return httpx.Response(200, json={"result": {"items": [], "last_id": "", "total": 3}})

    assert real_client(handler).product_list()[2] == 3
