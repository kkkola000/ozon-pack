"""Сборка заказов Avito: стикер открывает заказ, штрихкод товара пишется в отчёт."""
import pytest

from app import avito_pack, db, report, store


def orders(account, status="ready_to_ship"):
    return [dict(r) for r in db.query(
        "SELECT * FROM avito_orders WHERE account_id = ? AND status = ? ORDER BY id",
        (account["id"], status))]


def shipped(status=None):
    sql = "SELECT * FROM shipped_items"
    params = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    return [dict(r) for r in db.query(sql + " ORDER BY id", params)]


@pytest.fixture
def avito_data(avito_account):
    from app import sync

    sync.sync_avito(avito_account)
    ready = orders(avito_account)
    assert ready, "в подделке Avito нет заказов «Отправьте заказ»"
    return ready


# ------------------------------------------------------------------ опознание стикера
def test_label_is_found_by_any_number(avito_account, avito_data):
    order = avito_data[0]
    for code in (order["id"], order["marketplace_id"]):
        if not code:
            continue
        found = avito_pack.find_order(avito_account["id"], str(code))
        assert found and found["id"] == order["id"], code


def test_unknown_label_is_refused(avito_account, avito_data, user):
    result = avito_pack.scan(avito_account, user, "НЕТ-ТАКОГО-СТИКЕРА")
    assert result["status"] == "error"
    assert result["action"] == "unknown"
    assert result["state"]["active"] is None
    assert shipped() == []


# ------------------------------------------------------------------ порядок работы
def test_full_flow_label_then_items(avito_account, avito_data, user):
    order = avito_data[0]

    opened = avito_pack.scan(avito_account, user, order["marketplace_id"] or order["id"])
    assert opened["action"] == "order_opened"
    state = opened["state"]
    assert state["active"]["id"] == order["id"]
    assert state["total"] >= 1
    # Открытие заказа само по себе в отчёт не пишется
    assert shipped() == []

    codes = [f"MY-BARCODE-{i}" for i in range(1, state["total"] + 1)]
    for index, code in enumerate(codes, start=1):
        result = avito_pack.scan(avito_account, user, code)
        assert result["status"] == "ok", result["message"]
        if index < len(codes):
            assert result["action"] == "item_scanned"

    assert result["action"] == "completed"
    rows = shipped("ok")
    assert len(rows) == len(codes)
    assert [r["barcode"] for r in rows] == codes
    assert all(r["posting_number"] == order["id"] for r in rows)
    assert all(r["marketplace"] == "avito" for r in rows)
    assert all(r["name"] for r in rows), "название товара должно попадать в отчёт"

    row = db.query_one("SELECT * FROM avito_orders WHERE account_id = ? AND id = ?",
                       (avito_account["id"], order["id"]))
    assert row["local_state"] == "packed"
    assert row["packed_by"] == user["login"]
    assert row["claim_user_id"] is None
    assert avito_pack.load_state(avito_account, user)["active"] is None


def test_item_scan_without_open_order_is_refused(avito_account, avito_data, user):
    result = avito_pack.scan(avito_account, user, "ПРОСТО-ШТРИХКОД")
    assert result["status"] == "error"
    assert "стикер отправления" in result["message"]
    assert shipped() == []


def test_wrong_label_while_packing(avito_account, avito_data, user):
    if len(avito_data) < 2:
        pytest.skip("нужно хотя бы два заказа «Отправьте заказ»")
    first, other = avito_data[0], avito_data[1]
    avito_pack.scan(avito_account, user, first["id"])

    result = avito_pack.scan(avito_account, user, other["marketplace_id"] or other["id"])
    assert result["status"] == "error"
    assert result["action"] == "wrong_label"
    errors = shipped("error")
    assert len(errors) == 1
    assert errors[0]["reason"] == "wrong_label"
    assert errors[0]["posting_number"] == first["id"]


def test_label_rescan_before_all_items(avito_account, avito_data, user):
    """Пока отсканированы не все единицы, повторный скан стикера не закрывает заказ."""
    order = avito_data[0]
    # В заказе должно быть больше одной единицы — при необходимости добавляем позицию
    db.execute(
        "INSERT OR IGNORE INTO avito_order_items(account_id, order_id, avito_id, seller_id, title, quantity)"
        " VALUES(?,?,?,?,?,1)",
        (avito_account["id"], order["id"], "вторая-позиция", "ART-2", "Вторая позиция"),
    )
    state = avito_pack.scan(avito_account, user, order["id"])["state"]
    assert state["total"] >= 2

    avito_pack.scan(avito_account, user, "BC-1")
    result = avito_pack.scan(avito_account, user, order["id"])
    assert result["status"] == "warning"
    assert result["action"] == "incomplete"
    assert db.query_one("SELECT local_state FROM avito_orders WHERE account_id = ? AND id = ?",
                        (avito_account["id"], order["id"]))["local_state"] != "packed"


def test_second_pass_does_not_duplicate_report_rows(avito_account, avito_data, user):
    order = avito_data[0]
    state = avito_pack.scan(avito_account, user, order["id"])["state"]
    for i in range(state["total"]):
        avito_pack.scan(avito_account, user, f"BC-{i}")
    first_count = len(shipped("ok"))

    # Вернули заказ в работу и собрали заново теми же штрихкодами
    db.execute("UPDATE avito_orders SET local_state = 'new', packed_at = NULL, packed_by = NULL "
               "WHERE account_id = ? AND id = ?", (avito_account["id"], order["id"]))
    avito_pack.scan(avito_account, user, order["id"])
    for i in range(state["total"]):
        avito_pack.scan(avito_account, user, f"BC-{i}")

    assert len(shipped("ok")) == first_count, "повторная сборка не должна раздувать отчёт"


def test_packed_order_is_not_reopened(avito_account, avito_data, user):
    order = avito_data[0]
    state = avito_pack.scan(avito_account, user, order["id"])["state"]
    for i in range(state["total"]):
        avito_pack.scan(avito_account, user, f"BC-{i}")

    result = avito_pack.scan(avito_account, user, order["id"])
    assert result["status"] == "warning"
    assert result["action"] == "already_packed"


def test_order_awaiting_confirmation_is_not_packable(avito_account, avito_data, user):
    waiting = orders(avito_account, "on_confirmation")
    if not waiting:
        pytest.skip("в подделке нет заказов «Подтвердите заказ»")
    result = avito_pack.scan(avito_account, user, waiting[0]["id"])
    assert result["status"] == "warning"
    assert result["action"] == "wrong_status"
    assert result["state"]["active"] is None


def test_release_frees_the_order(avito_account, avito_data, user):
    order = avito_data[0]
    avito_pack.scan(avito_account, user, order["id"])
    avito_pack.release(avito_account, user)
    row = db.query_one("SELECT claim_user_id FROM avito_orders WHERE account_id = ? AND id = ?",
                       (avito_account["id"], order["id"]))
    assert row["claim_user_id"] is None
    assert avito_pack.load_state(avito_account, user)["active"] is None


def test_report_row_carries_scanned_barcode_and_seller_article(avito_account, avito_data, user):
    order = avito_data[0]
    avito_pack.scan(avito_account, user, order["id"])
    avito_pack.scan(avito_account, user, "СВОЙ-ШТРИХКОД-777")

    row = shipped("ok")[0]
    assert row["barcode"] == "СВОЙ-ШТРИХКОД-777"
    assert row["sku"] is None, "у Avito SKU нет"
    items = store.avito_items(avito_account["id"], order["id"])
    assert row["name"] == items[0]["title"]
    assert row["report_date"] == report.report_date()


def test_units_are_filled_in_order(avito_account, avito_data, user):
    """Каждой единице — свой скан; штрихкоды ложатся по позициям по порядку."""
    order = avito_data[0]
    db.execute(
        "INSERT OR IGNORE INTO avito_order_items(account_id, order_id, avito_id, seller_id, title, quantity)"
        " VALUES(?,?,?,?,?,2)",
        (avito_account["id"], order["id"], "позиция-на-два", "ART-X", "Позиция в двух экземплярах"),
    )
    state = avito_pack.scan(avito_account, user, order["id"])["state"]
    total = state["total"]
    assert total >= 3

    for i in range(total):
        avito_pack.scan(avito_account, user, f"CODE-{i}")

    rows = shipped("ok")
    assert len(rows) == total
    assert [r["barcode"] for r in rows] == [f"CODE-{i}" for i in range(total)]
    # У позиции с количеством 2 появляются единицы 1 и 2
    двойная = [r for r in rows if r["offer_id"] == "ART-X"]
    assert sorted(r["unit_no"] for r in двойная) == [1, 2]
