"""Сборка заказов Маркета — по образцу Ozon: товар открывает заказ, ярлык закрывает."""
import pytest

from app.core import db, report, store, sync
from app.markets.yandex import client as yandex, pack as yandex_pack


@pytest.fixture
def market(yandex_account, sample_data):
    """Кабинет Маркета вместе с каталогом Ozon — без него штрихкодов не найти."""
    sync.sync_yandex(yandex_account)
    return yandex_account


def work_orders(account):
    rows = db.query(
        "SELECT * FROM yandex_orders WHERE account_id = ? AND local_state != 'packed' "
        "ORDER BY (shipment_date IS NULL), shipment_date, id",
        (account["id"],),
    )
    return [store.yandex_view(r) for r in rows]


def shipped(status=None):
    sql = "SELECT * FROM shipped_items"
    params = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    return [dict(r) for r in db.query(sql + " ORDER BY id", params)]


def scan_until_complete(account, user):
    state = yandex_pack.load_state(account, user)
    for _ in range(50):
        if state["complete"]:
            break
        missing = next(i for i in state["items"] if not i["ok"])
        result = yandex_pack.scan(account, user, missing["barcodes"][0])
        assert result["status"] == "ok", result["message"]
        state = yandex_pack.load_state(account, user)
    assert state["complete"], "состав так и не собрался"
    return state


# ------------------------------------------------------------------ полный путь
def test_product_scan_opens_the_most_urgent_order(market, user):
    first = work_orders(market)[0]
    barcode = first["items"][0]["barcodes"][0]

    result = yandex_pack.scan(market, user, barcode)
    assert result["action"] == "order_selected", result["message"]
    assert result["print"] == {"order_id": result["state"]["active"]["id"]}
    active = result["state"]["active"]
    assert any(i["offer_id"] == first["items"][0]["offer_id"] for i in active["items"])
    assert result["state"]["done"] == 1
    # Скан, открывший заказ, уже в отчёте: пара «штрихкод -> заказ» сошлась.
    rows = shipped("ok")
    assert len(rows) == 1
    assert rows[0]["posting_number"] == active["id"]
    assert rows[0]["marketplace"] == "yandex"
    assert rows[0]["barcode"] == barcode
    assert rows[0]["item_key"].startswith("ym:")

    row = db.query_one("SELECT claim_user_id, claim_login FROM yandex_orders WHERE id = ?", (active["id"],))
    assert row["claim_user_id"] == user["id"] and row["claim_login"] == user["login"]


def test_full_flow_ends_with_label_scan(market, user):
    first = work_orders(market)[0]
    yandex_pack.scan(market, user, first["items"][0]["barcodes"][0])
    state = scan_until_complete(market, user)
    order_id = state["active"]["id"]

    done = yandex_pack.scan(market, user, order_id)
    assert done["action"] == "completed", done["message"]
    assert done["state"]["active"] is None

    row = db.query_one("SELECT * FROM yandex_orders WHERE id = ?", (order_id,))
    assert row["local_state"] == "packed"
    assert row["packed_by"] == user["login"]
    assert row["claim_user_id"] is None
    assert len(shipped("ok")) == state["total"]
    assert all(r["report_date"] == report.report_date() for r in shipped("ok"))


def test_box_label_code_closes_the_order(market, user):
    """На ярлыке грузового места номер заказа идёт с номером места: «123-1»."""
    first = work_orders(market)[0]
    yandex_pack.scan(market, user, first["items"][0]["barcodes"][0])
    state = scan_until_complete(market, user)
    done = yandex_pack.scan(market, user, f"{state['active']['id']}-1")
    assert done["action"] == "completed", done["message"]


def test_label_scan_opens_the_order_without_printing(market, user):
    order = work_orders(market)[0]
    result = yandex_pack.scan(market, user, order["id"])
    assert result["action"] == "order_selected"
    assert result["print"] is None
    assert result["state"]["done"] == 0
    assert shipped() == []


def test_external_order_number_opens_the_order_too(market, user):
    order = work_orders(market)[0]
    result = yandex_pack.scan(market, user, order["external_id"])
    assert result["action"] == "order_selected"
    assert result["state"]["active"]["id"] == order["id"]


# ------------------------------------------------------------------ защита от ошибок
def test_wrong_product_is_blocked(market, user):
    order = work_orders(market)[0]
    yandex_pack.scan(market, user, order["id"])
    own = {i["offer_id"] for i in order["items"]}
    foreign = db.query_one(
        f"SELECT p.offer_id, b.barcode FROM products p JOIN product_barcodes b ON b.sku = p.sku "
        f"AND b.account_id = p.account_id WHERE p.offer_id NOT IN ({','.join('?' for _ in own)}) LIMIT 1",
        list(own),
    )
    result = yandex_pack.scan(market, user, foreign["barcode"])
    assert result["status"] == "error"
    assert result["action"] == "wrong_product"
    errors = shipped("error")
    assert len(errors) == 1 and errors[0]["reason"] == "wrong_product"
    assert errors[0]["posting_number"] == order["id"]
    assert yandex_pack.load_state(market, user)["done"] == 0


def test_extra_scan_is_refused(market, user):
    order = work_orders(market)[0]
    yandex_pack.scan(market, user, order["id"])
    state = scan_until_complete(market, user)
    item = state["items"][0]
    result = yandex_pack.scan(market, user, item["barcodes"][0])
    assert result["status"] == "warning"
    assert result["action"] == "extra_product"
    assert yandex_pack.load_state(market, user)["done"] == state["total"]
    assert len(shipped("ok")) == state["total"]


def test_label_before_all_items_does_not_close(market, user):
    orders = [o for o in work_orders(market) if o["items_count"] >= 2]
    if not orders:
        pytest.skip("нужен заказ хотя бы из двух единиц")
    order = orders[0]
    yandex_pack.scan(market, user, order["id"])
    yandex_pack.scan(market, user, order["items"][0]["barcodes"][0])
    result = yandex_pack.scan(market, user, order["id"])
    assert result["status"] == "warning"
    assert result["action"] == "incomplete"
    assert "Осталось" in result["message"]
    assert db.query_one("SELECT local_state FROM yandex_orders WHERE id = ?", (order["id"],))["local_state"] == "new"


def test_wrong_label_while_packing(market, user):
    first, other = work_orders(market)[:2]
    yandex_pack.scan(market, user, first["id"])
    result = yandex_pack.scan(market, user, other["id"])
    assert result["status"] == "error"
    assert result["action"] == "wrong_label"
    errors = shipped("error")
    assert len(errors) == 1 and errors[0]["reason"] == "wrong_label"
    assert errors[0]["posting_number"] == first["id"]
    assert yandex_pack.load_state(market, user)["active"]["id"] == first["id"]


def test_packed_order_is_not_reopened(market, user):
    order = work_orders(market)[0]
    yandex_pack.scan(market, user, order["id"])
    scan_until_complete(market, user)
    yandex_pack.scan(market, user, order["id"])

    result = yandex_pack.scan(market, user, order["id"])
    assert result["status"] == "warning"
    assert result["action"] == "already_packed"
    assert result["state"]["active"] is None


def test_packed_orders_leave_the_candidate_list(market, user):
    """Товар нужен в нескольких заказах: собранный выпадает, следующий скан берёт следующий."""
    orders = work_orders(market)
    offer = orders[0]["items"][0]["offer_id"]
    barcode = orders[0]["items"][0]["barcodes"][0]
    with_offer = [o for o in orders if any(i["offer_id"] == offer for i in o["items"])]
    if len(with_offer) < 2:
        pytest.skip("нужен товар, который стоит в двух заказах")

    first = yandex_pack.scan(market, user, barcode)
    assert "нужен ещё в" in first["message"]
    first_id = first["state"]["active"]["id"]
    scan_until_complete(market, user)
    yandex_pack.scan(market, user, first_id)

    second = yandex_pack.scan(market, user, barcode)
    assert second["action"] == "order_selected"
    assert second["state"]["active"]["id"] != first_id


def test_order_claimed_by_someone_else_is_locked(market, user, other_user):
    order = work_orders(market)[0]
    yandex_pack.scan(market, other_user, order["id"])
    result = yandex_pack.scan(market, user, order["id"])
    assert result["status"] == "error"
    assert result["action"] == "locked"
    assert other_user["login"] in result["message"]


def test_release_frees_the_order(market, user):
    order = work_orders(market)[0]
    yandex_pack.scan(market, user, order["id"])
    result = yandex_pack.release(market, user)
    assert result["action"] == "released"
    row = db.query_one("SELECT claim_user_id FROM yandex_orders WHERE id = ?", (order["id"],))
    assert row["claim_user_id"] is None
    assert yandex_pack.load_state(market, user)["active"] is None
    assert db.query_one("SELECT 1 FROM events WHERE kind = 'yandex_pack_release'")


def test_unknown_code_goes_to_the_report(market, user):
    result = yandex_pack.scan(market, user, "НЕТ-ТАКОГО-КОДА")
    assert result["status"] == "error"
    assert result["action"] == "unknown"
    assert "каталога Ozon" in result["message"]
    errors = shipped("error")
    assert len(errors) == 1 and errors[0]["reason"] == "unknown_barcode"

    missing = yandex_pack.scan(market, user, "99999999")
    assert missing["action"] == "unknown"
    assert "не найден в панели" in missing["message"]


def test_product_that_no_order_needs(market, user):
    row = db.query_one(
        "SELECT b.barcode FROM product_barcodes b JOIN products p ON p.sku = b.sku AND p.account_id = b.account_id "
        "WHERE p.offer_id NOT IN (SELECT offer_id FROM yandex_order_items) LIMIT 1"
    )
    result = yandex_pack.scan(market, user, row["barcode"])
    assert result["status"] == "error"
    assert result["action"] == "no_candidates"


def test_second_pass_does_not_duplicate_report_rows(market, user):
    order = work_orders(market)[0]
    yandex_pack.scan(market, user, order["id"])
    scan_until_complete(market, user)
    yandex_pack.scan(market, user, order["id"])
    first_count = len(shipped("ok"))

    db.execute("UPDATE yandex_orders SET local_state = 'new', packed_at = NULL, packed_by = NULL WHERE id = ?",
               (order["id"],))
    yandex_pack.scan(market, user, order["id"])
    scan_until_complete(market, user)
    assert len(shipped("ok")) == first_count, "повторная сборка не должна раздувать отчёт"


def test_ready_to_ship_orders_are_packable_too(market, user):
    ready = [o for o in work_orders(market) if o["substatus"] == yandex.SUBSTATUS_READY_TO_SHIP]
    assert ready
    result = yandex_pack.scan(market, user, ready[0]["id"])
    assert result["action"] == "order_selected"


def test_switching_cabinet_releases_the_previous_claim(market, user, account):
    """Одна строка pack_state на сборщика: бронь в кабинете Ozon отпускается."""
    from app.markets.ozon import pack as packing
    from tests.conftest import pick_posting

    posting = pick_posting(positions=1)
    packing.select_posting(account, user, posting["posting_number"])
    order = work_orders(market)[0]
    yandex_pack.scan(market, user, order["id"])
    row = db.query_one("SELECT claim_user_id FROM postings WHERE posting_number = ?", (posting["posting_number"],))
    assert row["claim_user_id"] is None
