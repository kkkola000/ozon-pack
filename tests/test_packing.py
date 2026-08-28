"""Сценарии рабочего места сборщика — то, ради чего вся панель."""
import pytest

from app import db, packing, store
from app.config import settings
from tests.conftest import barcode_of, pick_posting


def scan_all_items(user, posting):
    """Отсканировать полный состав отправления."""
    for item in posting["items"]:
        for _ in range(item["quantity"]):
            result = packing.scan(user, barcode_of(item["sku"]))
            assert result["status"] in ("ok", "choose"), result["message"]
    return packing.load_state(user)


def test_full_flow_single_item(demo_data, user):
    posting = pick_posting(positions=1)
    sku = posting["items"][0]["sku"]

    result = packing.scan(user, barcode_of(sku))
    if result["action"] == "need_choice":
        result = packing.select_posting(user, posting["posting_number"], first_sku=sku)
    assert result["action"] == "posting_selected"
    assert result["print"]["posting_number"] == result["state"]["active"]["posting_number"]

    active = result["state"]["active"]["posting_number"]
    state = packing.load_state(user)
    while not state["complete"]:
        packing.scan(user, barcode_of(state["items"][0]["sku"]))
        state = packing.load_state(user)

    done = packing.scan(user, active)
    assert done["action"] == "completed"
    assert done["state"]["active"] is None

    row = db.query_one("SELECT * FROM postings WHERE posting_number = ?", (active,))
    assert row["local_state"] == "packed"
    assert row["packed_by"] == user["login"]
    assert row["claim_user_id"] is None


def test_wrong_product_is_blocked(demo_data, user):
    posting = pick_posting(positions=1)
    packing.select_posting(user, posting["posting_number"])
    foreign = db.query_one(
        "SELECT sku FROM posting_items WHERE posting_number != ? AND sku NOT IN "
        "(SELECT sku FROM posting_items WHERE posting_number = ?) LIMIT 1",
        (posting["posting_number"], posting["posting_number"]),
    )["sku"]

    result = packing.scan(user, barcode_of(foreign))
    assert result["status"] == "error"
    assert result["action"] == "wrong_product"
    assert packing.load_state(user)["done"] == 0
    assert db.query_one("SELECT COUNT(*) c FROM events WHERE kind = 'scan_wrong_product'")["c"] == 1


def test_extra_scan_of_same_product_warns(demo_data, user):
    posting = pick_posting(positions=1)
    sku = posting["items"][0]["sku"]
    quantity = posting["items"][0]["quantity"]
    packing.select_posting(user, posting["posting_number"])
    for _ in range(quantity):
        packing.scan(user, barcode_of(sku))

    result = packing.scan(user, barcode_of(sku))
    assert result["action"] == "extra_product"
    assert result["sound"] == "error"
    assert packing.load_state(user)["done"] == quantity


def test_wrong_label_is_blocked(demo_data, user):
    first = pick_posting(positions=1)
    packing.select_posting(user, first["posting_number"])
    other = db.query_one(
        "SELECT posting_number FROM postings WHERE status = ? AND posting_number != ? LIMIT 1",
        (store.STATUS_AWAITING_DELIVER, first["posting_number"]),
    )["posting_number"]

    result = packing.scan(user, other)
    assert result["status"] == "error"
    assert result["action"] == "wrong_label"
    assert packing.load_state(user)["active"]["posting_number"] == first["posting_number"]


def test_label_before_all_items_is_blocked(demo_data, user):
    row = db.query_one(
        "SELECT * FROM postings WHERE status = ? AND items_count > 1 AND local_state = 'new' LIMIT 1",
        (store.STATUS_AWAITING_DELIVER,),
    )
    if not row:
        pytest.skip("в демо-данных нет многопозиционного отправления")
    posting = store.posting_view(row)
    packing.select_posting(user, posting["posting_number"])

    result = packing.scan(user, posting["posting_number"])
    assert result["action"] == "incomplete"
    assert db.query_one("SELECT local_state FROM postings WHERE posting_number = ?", (posting["posting_number"],))["local_state"] == "new"


def test_double_assembly_is_blocked(demo_data, user):
    posting = pick_posting(positions=1)
    packing.select_posting(user, posting["posting_number"])
    scan_all_items(user, posting)
    packing.scan(user, posting["posting_number"])

    again = packing.scan(user, posting["posting_number"])
    assert again["action"] == "already_packed"
    assert again["sound"] == "error"


def test_packed_posting_not_offered_again(demo_data, user):
    posting = pick_posting(positions=1)
    sku = posting["items"][0]["sku"]
    packing.select_posting(user, posting["posting_number"])
    scan_all_items(user, posting)
    packing.scan(user, posting["posting_number"])

    candidates = packing.candidates_for_sku(sku, user)
    assert posting["posting_number"] not in [c["posting_number"] for c in candidates]


def test_claim_blocks_second_packer(demo_data, user, other_user):
    posting = pick_posting(positions=1)
    packing.select_posting(user, posting["posting_number"])

    result = packing.select_posting(other_user, posting["posting_number"])
    assert result["action"] == "locked"
    assert user["login"] in result["message"]


def test_awaiting_packaging_requires_ship_first(demo_data, user):
    posting = pick_posting(status=store.STATUS_AWAITING_PACKAGING)
    result = packing.scan(user, posting["posting_number"])
    assert result["action"] == "needs_ship"
    assert packing.load_state(user)["active"] is None


def test_auto_ship_on_scan_setting(demo_data, user, monkeypatch):
    monkeypatch.setattr(settings, "auto_ship_on_scan", True)
    posting = pick_posting(status=store.STATUS_AWAITING_PACKAGING)
    result = packing.scan(user, posting["posting_number"])
    assert result["action"] == "posting_selected"
    assert db.query_one("SELECT status FROM postings WHERE posting_number = ?", (posting["posting_number"],))["status"] == store.STATUS_AWAITING_DELIVER


def test_unknown_code(demo_data, user):
    result = packing.scan(user, "999999999999999")
    assert result["status"] == "error"
    assert result["action"] == "unknown"


def test_release_frees_posting(demo_data, user, other_user):
    posting = pick_posting(positions=1)
    packing.select_posting(user, posting["posting_number"])
    packing.release(user)

    assert packing.load_state(user)["active"] is None
    taken = packing.select_posting(other_user, posting["posting_number"])
    assert taken["action"] == "posting_selected"


def test_ship_moves_status(demo_data, user):
    posting = pick_posting(status=store.STATUS_AWAITING_PACKAGING)
    result = packing.ship_posting(user, posting["posting_number"])
    assert result["status"] == "ok"
    assert db.query_one("SELECT status FROM postings WHERE posting_number = ?", (posting["posting_number"],))["status"] == store.STATUS_AWAITING_DELIVER

    again = packing.ship_posting(user, posting["posting_number"])
    assert again["status"] == "ok"  # повторный вызов безопасен


def test_label_marks_print(demo_data, user):
    posting = pick_posting()
    pdf, name = packing.label_pdf(user, [posting["posting_number"]])
    assert pdf[:4] == b"%PDF"
    row = db.query_one("SELECT printed_at, print_count FROM postings WHERE posting_number = ?", (posting["posting_number"],))
    assert row["printed_at"] and row["print_count"] == 1


def test_switching_posting_releases_previous(demo_data, user, other_user):
    first = pick_posting(positions=1)
    packing.select_posting(user, first["posting_number"])
    second = db.query_one(
        "SELECT posting_number FROM postings WHERE status = ? AND posting_number != ? LIMIT 1",
        (store.STATUS_AWAITING_DELIVER, first["posting_number"]),
    )["posting_number"]
    packing.select_posting(user, second)

    assert packing.load_state(user)["active"]["posting_number"] == second
    # первое отправление снова свободно
    assert packing.select_posting(other_user, first["posting_number"])["action"] == "posting_selected"
