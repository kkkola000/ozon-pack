"""Возвраты Яндекс Маркета: невыкупы и возвраты покупателей в пункте выдачи.

Метод — GET /v2/campaigns/{campaignId}/returns, по каждому магазину кабинета.
Статусов два и они фиксированные: READY_FOR_PICKUP — «К выдаче», лист для
поездки; PICKED — выдан магазину, из таких за число составляется акт. Вид
(невыкуп или возврат) не фильтруется. Устаревшие параметры (from_date, to_date,
refundAmount, partnerCompensation) панель не использует.

На посылке напечатан номер возврата — он и штрихкод на листе, и то, по чему
сканер узнаёт посылку при приёмке.
"""
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import db, return_acts, store
from app.main import app
from app.markets.yandex import client as yandex
from app.markets.yandex import returns as yandex_returns
from app.markets.yandex import sync as yandex_sync


@pytest.fixture
def fake(yandex_account):
    return yandex.get_client(yandex_account)


@pytest.fixture
def market(yandex_account, fake):
    yandex_sync.sync_account(yandex_account)
    return yandex_account


@pytest.fixture
def client(market):
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/returns"})
        page = test_client.get("/returns?tab=acts")
        test_client.headers["X-CSRF-Token"] = re.search(
            r'name="csrf-token" content="([^"]*)"', page.text).group(1)
        yield test_client


def rows(account, status=None):
    sql = "SELECT * FROM yandex_returns WHERE account_id = ?"
    params = [account["id"]]
    if status:
        sql += " AND shipment_status = ?"
        params.append(status)
    return [dict(row) for row in db.query(sql + " ORDER BY id", params)]


def fake_return(fake, status):
    return next(item for item in fake._returns if item["shipmentStatus"] == status)


# ------------------------------------------------------------------ методы API
def mock_client(handler) -> yandex.YandexClient:
    real = yandex.YandexClient("9000001", "tok", base_url="https://api.test", max_retries=1)
    real._client = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler))
    return real


def test_returns_request_uses_new_parameters():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"status": "OK", "result": {
            "returns": [{"id": 1, "shipmentStatus": "PICKED"}], "paging": {"nextPageToken": "next"}}})

    returns, token = mock_client(handler).returns(
        21000001, shipment_status="PICKED", from_date="2026-09-01", to_date="2026-09-15", page_token="p1")
    assert returns == [{"id": 1, "shipmentStatus": "PICKED"}] and token == "next"
    request = seen[0]
    assert request.method == "GET" and request.url.path == "/v2/campaigns/21000001/returns"
    params = dict(request.url.params)
    assert params["fromDate"] == "2026-09-01" and params["toDate"] == "2026-09-15"
    assert params["shipmentStatuses"] == "PICKED" and params["page_token"] == "p1"
    for old in ("from_date", "to_date"):
        assert old not in params, f"{old} Маркет отключает 12.10.2026"


def test_campaigns_are_only_of_this_business():
    def handler(request):
        page = int(request.url.params["page"])
        campaigns = ([{"id": 1, "business": {"id": 9000001}}, {"id": 2, "business": {"id": 777}}] if page == 1
                     else [{"id": 3, "business": {"id": 9000001}}])
        return httpx.Response(200, json={"campaigns": campaigns, "pager": {"currentPage": page, "pagesCount": 2}})

    found = mock_client(handler).campaigns()
    assert [campaign["id"] for campaign in found] == [1, 3], "чужие кабинеты ключа не берём"


# ------------------------------------------------------------------ загрузка
def test_ready_and_picked_are_stored_the_rest_is_not(market, fake):
    assert len(rows(market, yandex.RETURN_READY)) == 3
    picked = rows(market, yandex.RETURN_PICKED)
    assert len(picked) == 2
    assert all(row["received_day"] for row in picked), "выданному ставится день получения"
    assert not rows(market, "IN_TRANSIT"), "едущий возврат панели не нужен"
    assert {row["return_type"] for row in rows(market)} == {"RETURN", "UNREDEEMED"}, "и невыкупы, и возвраты"
    # «К выдаче» — без окна дат, выданные — окном; статус — в запросе.
    ready_requests = [r for r in fake.return_requests if r["shipmentStatuses"] == yandex.RETURN_READY]
    picked_requests = [r for r in fake.return_requests if r["shipmentStatuses"] == yandex.RETURN_PICKED]
    assert ready_requests and all(r["fromDate"] is None for r in ready_requests)
    assert picked_requests and all(r["fromDate"] and r["toDate"] for r in picked_requests)


def test_market_filter_is_not_trusted(yandex_account, fake):
    fake.ignore_filter = True
    yandex_returns.sync_returns(yandex_account)
    assert not rows(yandex_account, "IN_TRANSIT")
    assert len(rows(yandex_account, yandex.RETURN_READY)) == 3


def test_row_keeps_place_goods_amount_and_tracks(market, fake):
    raw = fake_return(fake, yandex.RETURN_READY)
    row = next(r for r in rows(market) if r["id"] == str(raw["id"]))
    assert row["place_name"] == raw["logisticPickupPoint"]["name"]
    assert "Екатеринбург" in row["place_address"]
    assert row["amount"] == raw["amount"]["value"], "сумма — из amount, а не refundAmount"
    assert row["items_count"] == raw["items"][0]["count"]
    assert row["order_id"] == str(raw["orderId"])
    if raw["returnType"] == "RETURN":
        assert f",TRK{raw['id']}," in row["tracks"]


def test_campaigns_come_from_orders_when_the_method_is_closed(yandex_account, fake):
    fake.campaigns_forbidden = True
    yandex_sync.sync_yandex(yandex_account)
    result = yandex_returns.sync_returns(yandex_account)
    assert result["yandex_returns"] == 3, result


def test_no_campaign_at_all_is_said(yandex_account, fake):
    fake.campaigns_forbidden = True
    result = yandex_returns.sync(yandex_account)
    assert "ни одного магазина" in result["message"]


def test_received_day_is_written_once(market, fake):
    raw = fake_return(fake, yandex.RETURN_PICKED)
    before = next(r for r in rows(market) if r["id"] == str(raw["id"]))["received_day"]
    later = datetime.now(timezone(timedelta(hours=3))) + timedelta(days=3)
    raw["updateDate"] = later.isoformat(timespec="seconds")
    yandex_returns.sync_returns(market)
    after = next(r for r in rows(market) if r["id"] == str(raw["id"]))["received_day"]
    assert after == before, "решение по деньгам двигает updateDate, но не день поездки"


def test_picked_up_return_leaves_the_ready_list(market, fake):
    first, second = [item for item in fake._returns if item["shipmentStatus"] == yandex.RETURN_READY][:2]
    first["shipmentStatus"] = yandex.RETURN_PICKED
    fake._returns.remove(second)
    result = yandex_returns.sync_returns(market)
    ready_ids = {row["id"] for row in rows(market, yandex.RETURN_READY)}
    assert str(first["id"]) not in ready_ids and str(second["id"]) not in ready_ids
    assert result.get("yandex_returns_gone") == 1
    moved = next(r for r in rows(market) if r["id"] == str(first["id"]))
    assert moved["shipment_status"] == yandex.RETURN_PICKED and moved["received_day"]


def test_goods_get_names_from_the_catalog(market):
    view = yandex_returns.ready([market["id"]])[0]
    assert view["goods"][0]["name"] and not view["goods"][0]["name"].startswith("Артикул ")
    assert view["type_label"] in ("Невыкуп", "Возврат")


# ------------------------------------------------------------------ раздел
def test_ready_list_and_sheet_print_the_return_number(client, market, fake):
    raw = fake_return(fake, yandex.RETURN_READY)
    page = client.get(f"/returns?shop={market['id']}").text
    assert str(raw["id"]) in page and raw["logisticPickupPoint"]["name"] in page
    assert "Невыкуп" in page and "Возврат" in page
    sheet = client.get(f"/returns/print?shop={market['id']}").text
    assert f'data-barcode="{raw["id"]}"' in sheet, "на листе штрихкод — номер возврата"
    pdf = client.get(f"/returns/sheet.pdf?shop={market['id']}")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


def test_place_and_search_filters(client, market, fake):
    raw = fake_return(fake, yandex.RETURN_READY)
    other = [item for item in fake._returns if item["shipmentStatus"] == yandex.RETURN_READY
             and item["logisticPickupPoint"]["name"] != raw["logisticPickupPoint"]["name"]][0]
    page = client.get("/returns", params={"shop": market["id"], "place": raw["logisticPickupPoint"]["name"]}).text
    assert str(raw["id"]) in page and str(other["id"]) not in page
    found = client.get("/returns", params={"shop": market["id"], "q": str(other["id"])}).text
    assert str(other["id"]) in found and str(raw["id"]) not in found


def make_act(client, market) -> str:
    day = rows(market, yandex.RETURN_PICKED)[0]["received_day"]
    made = client.post("/api/returns/acts/by-day", json={"day": day, "shop": str(market["id"])})
    assert made.status_code == 200, made.text
    return made.json()["act_id"]


def test_act_is_made_from_picked_returns(client, market):
    act_id = make_act(client, market)
    in_act = [row for row in rows(market) if row["act_id"] == act_id]
    assert {row["shipment_status"] for row in in_act} == {yandex.RETURN_PICKED}
    assert len(in_act) == len({row["id"] for row in rows(market, yandex.RETURN_PICKED)
                               if row["received_day"] == in_act[0]["received_day"]})
    again = client.post("/api/returns/acts/by-day",
                        json={"day": in_act[0]["received_day"], "shop": str(market["id"])}).json()
    assert not again.get("act_id"), "второй акт на те же возвраты не заводится"
    page = client.get("/returns?tab=acts").text
    assert 'data-marketplace="yandex"' in page


def test_mark_is_saved_for_a_market_return(client, market):
    act_id = make_act(client, market)
    row = next(row for row in rows(market) if row["act_id"] == act_id)
    saved = client.post("/api/returns/mark", json={"marketplace": "yandex", "id": row["id"],
                                                  "account_id": market["id"], "mark": "bad", "note": "мятая"})
    assert saved.status_code == 200, saved.text
    stored = db.query_one("SELECT mark, note FROM yandex_returns WHERE account_id = ? AND id = ?",
                          (market["id"], row["id"]))
    assert (stored["mark"], stored["note"]) == ("bad", "мятая")
    assert return_acts.detail(act_id)


# ------------------------------------------------------------------ приёмка сканером
def test_return_number_opens_the_return_and_the_product_accepts_it(client, market):
    act_id = make_act(client, market)
    row = next(row for row in rows(market) if row["act_id"] == act_id)
    body = client.post("/api/returns/scan", json={"code": row["id"]}).json()
    assert [found["id"] for found in body["rows"]] == [row["id"]]
    card = body["rows"][0]
    assert card["marketplace"] == "yandex"
    offer = db.json_list(row["items"])[0]["offer_id"]
    barcode = db.query_one("SELECT barcode FROM product_barcodes WHERE account_id = ? AND sku = ?",
                           (market["id"], offer))["barcode"]
    assert card["goods"][0]["expect"] == barcode

    entries = [{"marketplace": "yandex", "id": row["id"], "account_id": market["id"], "got": {}}]
    for _ in range(row["items_count"]):
        answer = client.post("/api/returns/scan/goods", json={"code": barcode, "rows": entries}).json()
        assert answer["status"] == "ok", answer
        entries[0]["got"] = answer["got"]
    assert answer["action"] == "accepted"
    assert db.query_one("SELECT mark FROM yandex_returns WHERE account_id = ? AND id = ?",
                        (market["id"], row["id"]))["mark"] == "ok"


def test_order_and_track_find_the_return_too(market):
    row = next(row for row in rows(market) if row["tracks"])
    track = row["tracks"].strip(",").split(",")[0]
    assert yandex_returns.find([market["id"]], track) == [(market["id"], row["id"])]
    assert (market["id"], row["id"]) in yandex_returns.find([market["id"]], row["order_id"])
    assert yandex_returns.find([market["id"]], track[:-1]) == [], "кусок трека — не трек"


def test_received_day_is_a_local_day(market):
    for row in rows(market, yandex.RETURN_PICKED):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["received_day"])
        assert row["received_day"] <= store.local_day()
