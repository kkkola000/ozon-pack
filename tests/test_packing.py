"""Сценарии рабочего места сборщика — то, ради чего вся панель."""
import pytest

from app import db, packing, store
from app.config import settings
from tests.conftest import barcode_of, pick_posting


def scan_all_items(account, user, posting):
    """Отсканировать полный состав отправления."""
    for item in posting["items"]:
        for _ in range(item["quantity"]):
            result = packing.scan(account, user, barcode_of(item["sku"]))
            assert result["status"] in ("ok", "choose"), result["message"]
    return packing.load_state(account, user)


def test_full_flow_single_item(account, demo_data, user):
    posting = pick_posting(positions=1)
    sku = posting["items"][0]["sku"]

    result = packing.scan(account, user, barcode_of(sku))
    if result["action"] == "need_choice":
        result = packing.select_posting(account, user, posting["posting_number"], first_sku=sku)
    assert result["action"] == "posting_selected"
    assert result["print"]["posting_number"] == result["state"]["active"]["posting_number"]

    active = result["state"]["active"]["posting_number"]
    state = packing.load_state(account, user)
    while not state["complete"]:
        packing.scan(account, user, barcode_of(state["items"][0]["sku"]))
        state = packing.load_state(account, user)

    done = packing.scan(account, user, active)
    assert done["action"] == "completed"
    assert done["state"]["active"] is None

    row = db.query_one("SELECT * FROM postings WHERE account_id = ? AND posting_number = ?", (account["id"], active))
    assert row["local_state"] == "packed"
    assert row["packed_by"] == user["login"]
    assert row["claim_user_id"] is None


def test_wrong_product_is_blocked(account, demo_data, user):
    posting = pick_posting(positions=1)
    packing.select_posting(account, user, posting["posting_number"])
    foreign = db.query_one(
        "SELECT sku FROM posting_items WHERE account_id = ? AND posting_number != ? AND sku NOT IN "
        "(SELECT sku FROM posting_items WHERE account_id = ? AND posting_number = ?) LIMIT 1",
        (account["id"], posting["posting_number"], account["id"], posting["posting_number"]),
    )["sku"]

    result = packing.scan(account, user, barcode_of(foreign))
    assert result["status"] == "error"
    assert result["action"] == "wrong_product"
    assert packing.load_state(account, user)["done"] == 0
    assert db.query_one("SELECT COUNT(*) c FROM events WHERE kind = 'scan_wrong_product'")["c"] == 1


def test_extra_scan_of_same_product_warns(account, demo_data, user):
    posting = pick_posting(positions=1)
    sku = posting["items"][0]["sku"]
    quantity = posting["items"][0]["quantity"]
    packing.select_posting(account, user, posting["posting_number"])
    for _ in range(quantity):
        packing.scan(account, user, barcode_of(sku))

    result = packing.scan(account, user, barcode_of(sku))
    assert result["action"] == "extra_product"
    assert result["sound"] == "error"
    assert packing.load_state(account, user)["done"] == quantity


def test_wrong_label_is_blocked(account, demo_data, user):
    first = pick_posting(positions=1)
    packing.select_posting(account, user, first["posting_number"])
    other = db.query_one(
        "SELECT posting_number FROM postings WHERE account_id = ? AND status = ? AND posting_number != ? LIMIT 1",
        (account["id"], store.STATUS_AWAITING_DELIVER, first["posting_number"]),
    )["posting_number"]

    result = packing.scan(account, user, other)
    assert result["status"] == "error"
    assert result["action"] == "wrong_label"
    assert packing.load_state(account, user)["active"]["posting_number"] == first["posting_number"]


def test_label_before_all_items_is_blocked(account, demo_data, user):
    row = db.query_one(
        "SELECT * FROM postings WHERE account_id = ? AND status = ? AND items_count > 1 AND local_state = 'new' LIMIT 1",
        (account["id"], store.STATUS_AWAITING_DELIVER),
    )
    if not row:
        pytest.skip("в демо-данных нет многопозиционного отправления")
    posting = store.posting_view(row)
    packing.select_posting(account, user, posting["posting_number"])

    result = packing.scan(account, user, posting["posting_number"])
    assert result["action"] == "incomplete"
    assert db.query_one("SELECT local_state FROM postings WHERE account_id = ? AND posting_number = ?", (account["id"], posting["posting_number"]))["local_state"] == "new"


def test_double_assembly_is_blocked(account, demo_data, user):
    posting = pick_posting(positions=1)
    packing.select_posting(account, user, posting["posting_number"])
    scan_all_items(account, user, posting)
    packing.scan(account, user, posting["posting_number"])

    again = packing.scan(account, user, posting["posting_number"])
    assert again["action"] == "already_packed"
    assert again["sound"] == "error"


def test_packed_posting_not_offered_again(account, demo_data, user):
    posting = pick_posting(positions=1)
    sku = posting["items"][0]["sku"]
    packing.select_posting(account, user, posting["posting_number"])
    scan_all_items(account, user, posting)
    packing.scan(account, user, posting["posting_number"])

    candidates = packing.candidates_for_sku(account, sku, user)
    assert posting["posting_number"] not in [c["posting_number"] for c in candidates]


def test_claim_blocks_second_packer(account, demo_data, user, other_user):
    posting = pick_posting(positions=1)
    packing.select_posting(account, user, posting["posting_number"])

    result = packing.select_posting(account, other_user, posting["posting_number"])
    assert result["action"] == "locked"
    assert user["login"] in result["message"]


def test_awaiting_packaging_requires_ship_first(account, demo_data, user):
    posting = pick_posting(status=store.STATUS_AWAITING_PACKAGING)
    result = packing.scan(account, user, posting["posting_number"])
    assert result["action"] == "needs_ship"
    assert packing.load_state(account, user)["active"] is None


def test_auto_ship_on_scan_setting(account, demo_data, user, monkeypatch):
    monkeypatch.setattr(settings, "auto_ship_on_scan", True)
    posting = pick_posting(status=store.STATUS_AWAITING_PACKAGING)
    result = packing.scan(account, user, posting["posting_number"])
    assert result["action"] == "posting_selected"
    assert db.query_one("SELECT status FROM postings WHERE account_id = ? AND posting_number = ?", (account["id"], posting["posting_number"]))["status"] == store.STATUS_AWAITING_DELIVER


def test_unknown_code(account, demo_data, user):
    result = packing.scan(account, user, "999999999999999")
    assert result["status"] == "error"
    assert result["action"] == "unknown"


def test_release_frees_posting(account, demo_data, user, other_user):
    posting = pick_posting(positions=1)
    packing.select_posting(account, user, posting["posting_number"])
    packing.release(account, user)

    assert packing.load_state(account, user)["active"] is None
    taken = packing.select_posting(account, other_user, posting["posting_number"])
    assert taken["action"] == "posting_selected"


def test_ship_moves_status(account, demo_data, user):
    posting = pick_posting(status=store.STATUS_AWAITING_PACKAGING)
    result = packing.ship_posting(account, user, posting["posting_number"])
    assert result["status"] == "ok"
    assert db.query_one("SELECT status FROM postings WHERE account_id = ? AND posting_number = ?", (account["id"], posting["posting_number"]))["status"] == store.STATUS_AWAITING_DELIVER

    again = packing.ship_posting(account, user, posting["posting_number"])
    assert again["status"] == "ok"  # повторный вызов безопасен


def test_label_marks_print(account, demo_data, user):
    posting = pick_posting()
    pdf, name = packing.label_pdf(account, user, [posting["posting_number"]])
    assert pdf[:4] == b"%PDF"
    row = db.query_one("SELECT printed_at, print_count FROM postings WHERE account_id = ? AND posting_number = ?", (account["id"], posting["posting_number"]))
    assert row["printed_at"] and row["print_count"] == 1


def test_switching_posting_releases_previous(account, demo_data, user, other_user):
    first = pick_posting(positions=1)
    packing.select_posting(account, user, first["posting_number"])
    second = db.query_one(
        "SELECT posting_number FROM postings WHERE account_id = ? AND status = ? AND posting_number != ? LIMIT 1",
        (account["id"], store.STATUS_AWAITING_DELIVER, first["posting_number"]),
    )["posting_number"]
    packing.select_posting(account, user, second)

    assert packing.load_state(account, user)["active"]["posting_number"] == second
    # первое отправление снова свободно
    assert packing.select_posting(account, other_user, first["posting_number"])["action"] == "posting_selected"


def test_packed_posting_leaves_list_after_shipment(account, demo_data, user):
    """Отгруженное отправление не должно оставаться во вкладке «Собранные»."""
    from app import sync
    from app.routes.orders import _list_postings

    posting = pick_posting(positions=1)
    number = posting["posting_number"]
    packing.select_posting(account, user, number)
    scan_all_items(account, user, posting)
    packing.scan(account, user, number)

    packed = [p["posting_number"] for p in _list_postings(account, "packed")]
    assert number in packed, "сразу после сборки отправление должно быть в списке"

    # Ozon отгрузил отправление — статус ушёл из «Ожидает отгрузки»
    demo_data._postings[number]["status"] = "delivering"
    sync.sync_postings()

    assert db.query_one("SELECT status FROM postings WHERE account_id = ? AND posting_number = ?", (account["id"], number))["status"] == "delivering"
    packed_after = [p["posting_number"] for p in _list_postings(account, "packed")]
    assert number not in packed_after, "после отгрузки отправление должно уйти из списка"

    # Отметка о сборке и её автор сохраняются: это нужно для разбора спорных случаев
    row = db.query_one("SELECT local_state, packed_by FROM postings WHERE account_id = ? AND posting_number = ?", (account["id"], number))
    assert row["local_state"] == "packed" and row["packed_by"] == user["login"]


def test_cancelled_posting_leaves_packed_list(account, demo_data, user):
    """Отменённое отправление тоже не место в очереди на отгрузку."""
    import json

    from app import store
    from app.routes.orders import _list_postings

    posting = pick_posting(positions=1)
    number = posting["posting_number"]
    packing.select_posting(account, user, number)
    scan_all_items(account, user, posting)
    packing.scan(account, user, number)

    raw = json.loads(db.query_one("SELECT raw FROM postings WHERE account_id = ? AND posting_number = ?", (account["id"], number))["raw"])
    raw["status"] = "cancelled"
    with db.write() as conn:
        store.upsert_posting(conn, account["id"], raw)

    assert number not in [p["posting_number"] for p in _list_postings(account, "packed")]


def test_switching_cabinet_frees_the_claim(account, demo_data, user, other_user):
    """Сборщик ушёл в другой кабинет — отправление не должно висеть забронированным."""
    from app import accounts, sync

    posting = pick_posting()
    packing.select_posting(account, user, posting["posting_number"])
    assert db.query_one(
        "SELECT claim_login FROM postings WHERE account_id = ? AND posting_number = ?",
        (account["id"], posting["posting_number"]),
    )["claim_login"] == user["login"]

    second = accounts.get(accounts.create("ozon", "Второй магазин"))
    sync.sync_account(second)
    other = db.query_one(
        "SELECT posting_number FROM postings WHERE account_id = ? AND status = ? AND local_state = 'new' LIMIT 1",
        (second["id"], store.STATUS_AWAITING_DELIVER),
    )["posting_number"]
    packing.select_posting(second, user, other)

    freed = db.query_one(
        "SELECT claim_login FROM postings WHERE account_id = ? AND posting_number = ?",
        (account["id"], posting["posting_number"]),
    )["claim_login"]
    assert freed is None, "бронь в прежнем кабинете осталась"
    assert packing.select_posting(account, other_user, posting["posting_number"])["action"] == "posting_selected"
