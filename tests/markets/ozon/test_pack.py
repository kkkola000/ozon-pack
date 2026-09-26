"""Сценарии рабочего места сборщика — то, ради чего вся панель."""
import pytest

from app.core import db
from app.markets.ozon import pack as packing
from app.core.config import settings
from tests.conftest import barcode_of, pick_posting
from app.markets.ozon import store as ozon_store
from app.markets.ozon import sync as ozon_sync


def scan_all_items(account, user, posting):
    """Отсканировать полный состав отправления."""
    for item in posting["items"]:
        for _ in range(item["quantity"]):
            result = packing.scan(account, user, barcode_of(item["sku"]))
            assert result["status"] == "ok", result["message"]
    return packing.load_state(account, user)


def test_full_flow_single_item(account, sample_data, user):
    posting = pick_posting(positions=1)
    sku = posting["items"][0]["sku"]

    # Скан товара сразу берёт отправление: выбирать сборщику нечего, порядок
    # один — по сроку отгрузки. Какое именно взяли, читаем из состояния.
    result = packing.scan(account, user, barcode_of(sku))
    assert result["action"] == "posting_selected", result["message"]
    assert result["print"]["posting_number"] == result["state"]["active"]["posting_number"]

    active = result["state"]["active"]["posting_number"]
    state = packing.load_state(account, user)
    # Добираем то, чего не хватает, а не первую позицию вслепую: у взятого
    # отправления состав может быть не такой, как у того, что мы наметили.
    for _ in range(50):
        if state["complete"]:
            break
        missing = next(i for i in state["items"] if not i["ok"])
        packing.scan(account, user, barcode_of(missing["sku"]))
        state = packing.load_state(account, user)
    assert state["complete"], "состав так и не собрался"

    done = packing.scan(account, user, active)
    assert done["action"] == "completed"
    assert done["state"]["active"] is None

    row = db.query_one("SELECT * FROM postings WHERE account_id = ? AND posting_number = ?", (account["id"], active))
    assert row["local_state"] == "packed"
    assert row["packed_by"] == user["login"]
    assert row["claim_user_id"] is None


def test_wrong_product_is_blocked(account, sample_data, user):
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


def test_extra_scan_of_same_product_warns(account, sample_data, user):
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


def test_wrong_label_is_blocked(account, sample_data, user):
    first = pick_posting(positions=1)
    packing.select_posting(account, user, first["posting_number"])
    other = db.query_one(
        "SELECT posting_number FROM postings WHERE account_id = ? AND status = ? AND posting_number != ? LIMIT 1",
        (account["id"], ozon_store.STATUS_AWAITING_DELIVER, first["posting_number"]),
    )["posting_number"]

    result = packing.scan(account, user, other)
    assert result["status"] == "error"
    assert result["action"] == "wrong_label"
    assert packing.load_state(account, user)["active"]["posting_number"] == first["posting_number"]


def test_label_before_all_items_is_blocked(account, sample_data, user):
    row = db.query_one(
        "SELECT * FROM postings WHERE account_id = ? AND status = ? AND items_count > 1 AND local_state = 'new' LIMIT 1",
        (account["id"], ozon_store.STATUS_AWAITING_DELIVER),
    )
    if not row:
        pytest.skip("в демо-данных нет многопозиционного отправления")
    posting = ozon_store.posting_view(row)
    packing.select_posting(account, user, posting["posting_number"])

    result = packing.scan(account, user, posting["posting_number"])
    assert result["action"] == "incomplete"
    assert db.query_one("SELECT local_state FROM postings WHERE account_id = ? AND posting_number = ?", (account["id"], posting["posting_number"]))["local_state"] == "new"


def test_double_assembly_is_blocked(account, sample_data, user):
    posting = pick_posting(positions=1)
    packing.select_posting(account, user, posting["posting_number"])
    scan_all_items(account, user, posting)
    packing.scan(account, user, posting["posting_number"])

    again = packing.scan(account, user, posting["posting_number"])
    assert again["action"] == "already_packed"
    assert again["sound"] == "error"


def test_packed_posting_not_offered_again(account, sample_data, user):
    posting = pick_posting(positions=1)
    sku = posting["items"][0]["sku"]
    packing.select_posting(account, user, posting["posting_number"])
    scan_all_items(account, user, posting)
    packing.scan(account, user, posting["posting_number"])

    candidates = packing.candidates_for_sku(account, sku, user)
    assert posting["posting_number"] not in [c["posting_number"] for c in candidates]


def test_claim_blocks_second_packer(account, sample_data, user, other_user):
    posting = pick_posting(positions=1)
    packing.select_posting(account, user, posting["posting_number"])

    result = packing.select_posting(account, other_user, posting["posting_number"])
    assert result["action"] == "locked"
    assert user["login"] in result["message"]


def test_awaiting_packaging_requires_ship_first(account, sample_data, user):
    posting = pick_posting(status=ozon_store.STATUS_AWAITING_PACKAGING)
    result = packing.scan(account, user, posting["posting_number"])
    assert result["action"] == "needs_ship"
    assert packing.load_state(account, user)["active"] is None


def test_auto_ship_on_scan_setting(account, sample_data, user, monkeypatch):
    monkeypatch.setattr(settings, "auto_ship_on_scan", True)
    posting = pick_posting(status=ozon_store.STATUS_AWAITING_PACKAGING)
    result = packing.scan(account, user, posting["posting_number"])
    assert result["action"] == "posting_selected"
    assert db.query_one("SELECT status FROM postings WHERE account_id = ? AND posting_number = ?", (account["id"], posting["posting_number"]))["status"] == ozon_store.STATUS_AWAITING_DELIVER


def test_unknown_code(account, sample_data, user):
    result = packing.scan(account, user, "999999999999999")
    assert result["status"] == "error"
    assert result["action"] == "unknown"


def test_release_frees_posting(account, sample_data, user, other_user):
    posting = pick_posting(positions=1)
    packing.select_posting(account, user, posting["posting_number"])
    packing.release(account, user)

    assert packing.load_state(account, user)["active"] is None
    taken = packing.select_posting(account, other_user, posting["posting_number"])
    assert taken["action"] == "posting_selected"


def test_ship_moves_status(account, sample_data, user):
    posting = pick_posting(status=ozon_store.STATUS_AWAITING_PACKAGING)
    result = packing.ship_posting(account, user, posting["posting_number"])
    assert result["status"] == "ok"
    assert db.query_one("SELECT status FROM postings WHERE account_id = ? AND posting_number = ?", (account["id"], posting["posting_number"]))["status"] == ozon_store.STATUS_AWAITING_DELIVER

    again = packing.ship_posting(account, user, posting["posting_number"])
    assert again["status"] == "ok"  # повторный вызов безопасен


def test_label_marks_print(account, sample_data, user):
    posting = pick_posting()
    pdf, name = packing.labels(account, user, [posting["posting_number"]])
    assert pdf[:4] == b"%PDF"
    row = db.query_one("SELECT printed_at, print_count FROM postings WHERE account_id = ? AND posting_number = ?", (account["id"], posting["posting_number"]))
    assert row["printed_at"] and row["print_count"] == 1


def test_switching_posting_releases_previous(account, sample_data, user, other_user):
    first = pick_posting(positions=1)
    packing.select_posting(account, user, first["posting_number"])
    second = db.query_one(
        "SELECT posting_number FROM postings WHERE account_id = ? AND status = ? AND posting_number != ? LIMIT 1",
        (account["id"], ozon_store.STATUS_AWAITING_DELIVER, first["posting_number"]),
    )["posting_number"]
    packing.select_posting(account, user, second)

    assert packing.load_state(account, user)["active"]["posting_number"] == second
    # первое отправление снова свободно
    assert packing.select_posting(account, other_user, first["posting_number"])["action"] == "posting_selected"


def test_packed_posting_leaves_list_after_shipment(account, sample_data, user):
    """Отгруженное отправление не должно оставаться в статусе «Собран» раздела «Заказы»."""
    from app.core import board

    posting = pick_posting(positions=1)
    number = posting["posting_number"]
    packing.select_posting(account, user, number)
    scan_all_items(account, user, posting)
    packing.scan(account, user, number)

    packed = [p["number"] for p in board.rows([account], "packed")]
    assert number in packed, "сразу после сборки отправление должно быть в списке"

    # Ozon отгрузил отправление — статус ушёл из «Ожидает отгрузки»
    sample_data._postings[number]["status"] = "delivering"
    ozon_sync.sync_postings()

    assert db.query_one("SELECT status FROM postings WHERE account_id = ? AND posting_number = ?", (account["id"], number))["status"] == "delivering"
    packed_after = [p["number"] for p in board.rows([account], "packed")]
    assert number not in packed_after, "после отгрузки отправление должно уйти из списка"

    # Отметка о сборке и её автор сохраняются: это нужно для разбора спорных случаев
    row = db.query_one("SELECT local_state, packed_by FROM postings WHERE account_id = ? AND posting_number = ?", (account["id"], number))
    assert row["local_state"] == "packed" and row["packed_by"] == user["login"]


def test_cancelled_posting_leaves_packed_list(account, sample_data, user):
    """Отменённое отправление тоже не место в очереди на отгрузку."""
    import json

    from app.core import board

    posting = pick_posting(positions=1)
    number = posting["posting_number"]
    packing.select_posting(account, user, number)
    scan_all_items(account, user, posting)
    packing.scan(account, user, number)

    raw = json.loads(db.query_one("SELECT raw FROM postings WHERE account_id = ? AND posting_number = ?", (account["id"], number))["raw"])
    raw["status"] = "cancelled"
    with db.write() as conn:
        ozon_store.upsert_posting(conn, account["id"], raw)

    assert number not in [p["number"] for p in board.rows([account], "packed")]


def test_switching_cabinet_frees_the_claim(account, sample_data, user, other_user):
    """Сборщик ушёл в другой кабинет — отправление не должно висеть забронированным."""
    from app.core import accounts, sync

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
        (second["id"], ozon_store.STATUS_AWAITING_DELIVER),
    )["posting_number"]
    packing.select_posting(second, user, other)

    freed = db.query_one(
        "SELECT claim_login FROM postings WHERE account_id = ? AND posting_number = ?",
        (account["id"], posting["posting_number"]),
    )["claim_login"]
    assert freed is None, "бронь в прежнем кабинете осталась"
    assert packing.select_posting(account, other_user, posting["posting_number"])["action"] == "posting_selected"


# ---------------------------------------------------------------- когда печатать стикер
# Стикер печатается, только если его нет на руках. Сборщик, открывший
# отправление сканом самого стикера, уже держит его — печатать второй раз
# значит выдать лишнюю бумагу и дать повод перепутать наклейки.

def test_label_scan_does_not_reprint(account, sample_data, user):
    posting = pick_posting(positions=1)

    result = packing.scan(account, user, posting["posting_number"])
    assert result["action"] == "posting_selected", result["message"]
    assert result["print"] is None, "стикер ушёл на печать, хотя он уже в руках"


def test_product_scan_prints_label(account, sample_data, user):
    """Открыли сканом товара — стикера на руках нет, печатаем."""
    posting = pick_posting(positions=1)
    sku = posting["items"][0]["sku"]

    result = packing.scan(account, user, barcode_of(sku))
    assert result["action"] == "posting_selected", result["message"]
    assert result["print"]["posting_number"] == result["state"]["active"]["posting_number"]


def test_select_posting_prints(account, sample_data, user):
    """Отправление взяли в сборку не сканом стикера — стикера на руках нет."""
    posting = pick_posting(positions=1)
    result = packing.select_posting(account, user, posting["posting_number"])
    assert result["print"]["posting_number"] == posting["posting_number"]


def test_no_print_while_a_posting_is_open(account, sample_data, user):
    """При открытой сборке ни один скан стикер на печать не отправляет.

    Сборщик жаловался ровно на это: подтверждает товар — печатается второй
    стикер, сканирует стикер — печатается третий. Печать допустима в одном
    случае — скан штрихкода товара на свободном рабочем месте.
    """
    posting = pick_posting(positions=1)
    number = posting["posting_number"]
    packing.select_posting(account, user, number)

    state = packing.load_state(account, user)
    for _ in range(50):
        if state["complete"]:
            break
        missing = next(i for i in state["items"] if not i["ok"])
        result = packing.scan(account, user, barcode_of(missing["sku"]))
        assert result.get("print") is None, "стикер ушёл на печать при скане товара в открытой сборке"
        state = packing.load_state(account, user)

    # Скан стикера в открытой сборке закрывает её — и тоже ничего не печатает.
    done = packing.scan(account, user, number)
    assert done["action"] == "completed", done["message"]
    assert done.get("print") is None, "стикер ушёл на печать при закрытии сборки"


def test_no_print_on_wrong_scans_in_open_posting(account, sample_data, user):
    """Ошибочные сканы при открытой сборке печать тоже не запускают."""
    posting = pick_posting(positions=1)
    number = posting["posting_number"]
    packing.select_posting(account, user, number)

    foreign_sku = db.query_one(
        "SELECT sku FROM posting_items WHERE account_id = ? AND posting_number != ? AND sku NOT IN "
        "(SELECT sku FROM posting_items WHERE account_id = ? AND posting_number = ?) LIMIT 1",
        (account["id"], number, account["id"], number),
    )["sku"]
    other_number = db.query_one(
        "SELECT posting_number FROM postings WHERE account_id = ? AND posting_number != ? "
        "AND status = ? AND local_state = 'new' LIMIT 1",
        (account["id"], number, ozon_store.STATUS_AWAITING_DELIVER),
    )["posting_number"]

    for code in (foreign_sku and barcode_of(foreign_sku), other_number, "нет-такого-кода"):
        result = packing.scan(account, user, code)
        assert result.get("print") is None, f"скан «{code}» отправил стикер на печать"
    assert packing.load_state(account, user)["active"]["posting_number"] == number


def test_reselecting_open_posting_does_not_reprint(account, sample_data, user):
    """Повторный вход в уже открытую сборку — стикер для неё уже печатался."""
    posting = pick_posting(positions=1)
    number = posting["posting_number"]
    assert packing.select_posting(account, user, number)["print"]["posting_number"] == number

    again = packing.select_posting(account, user, number)
    assert again["action"] == "posting_selected", again["message"]
    assert again["print"] is None, "стикер ушёл на печать повторно"


def test_whole_flow_started_from_label(account, sample_data, user):
    """Скан стикера, товары, снова стикер — сборка закрывается, печати нет."""
    posting = pick_posting(positions=1)
    number = posting["posting_number"]

    opened = packing.scan(account, user, number)
    assert opened["print"] is None

    state = scan_all_items(account, user, posting)
    assert state["complete"], "не все товары отсканировались"

    done = packing.scan(account, user, number)
    assert done["action"] == "completed", done["message"]
    assert done.get("print") is None, "стикер печатается при завершении сборки"

    row = db.query_one(
        "SELECT local_state, print_count FROM postings WHERE account_id = ? AND posting_number = ?",
        (account["id"], number),
    )
    assert row["local_state"] == "packed"
    assert row["print_count"] == 0, "стикер печатался, хотя сборку начали с его скана"


# ------------------------------------------------- товар в нескольких отправлениях
# Раньше такой скан показывал список и ждал выбора. Выбирать там нечего:
# порядок всё равно один — по сроку отгрузки. Панель берёт самое срочное и
# печатает его стикер, а собранное выпадает из подбора само, так что следующий
# скан того же штрихкода отдаёт следующее отправление.

def sku_in_several_postings(account, user):
    """SKU, который нужен минимум в двух отправлениях к отгрузке."""
    row = db.query_one(
        """
        SELECT i.sku FROM posting_items i
        JOIN postings p ON p.posting_number = i.posting_number AND p.account_id = i.account_id
        WHERE i.account_id = ? AND p.status = ? AND p.local_state = 'new'
        GROUP BY i.sku HAVING COUNT(DISTINCT i.posting_number) > 1
        LIMIT 1
        """,
        (account["id"], ozon_store.STATUS_AWAITING_DELIVER),
    )
    if not row:
        pytest.skip("в демо-данных нет товара сразу в нескольких отправлениях")
    return row["sku"]


def test_scan_takes_the_most_urgent_and_prints(account, sample_data, user):
    sku = sku_in_several_postings(account, user)
    expected = packing.candidates_for_sku(account, sku, user)
    assert len(expected) > 1

    result = packing.scan(account, user, barcode_of(sku))

    assert result["action"] == "posting_selected", result["message"]
    # Взято первое из очереди — она отсортирована по сроку отгрузки
    assert result["state"]["active"]["posting_number"] == expected[0]["posting_number"]
    assert result["print"]["posting_number"] == expected[0]["posting_number"]
    # И сказано, сколько ещё впереди
    assert "ещё" in result["message"], result["message"]


def test_next_scan_takes_the_next_posting(account, sample_data, user):
    """Собрали одно — тот же штрихкод отдаёт следующее, а не то же самое."""
    sku = sku_in_several_postings(account, user)
    queue = [c["posting_number"] for c in packing.candidates_for_sku(account, sku, user)]

    first = packing.scan(account, user, barcode_of(sku))
    assert first["state"]["active"]["posting_number"] == queue[0]

    # Закрываем первое отправление целиком
    state = packing.load_state(account, user)
    for _ in range(50):
        if state["complete"]:
            break
        missing = next(i for i in state["items"] if not i["ok"])
        packing.scan(account, user, barcode_of(missing["sku"]))
        state = packing.load_state(account, user)
    assert packing.scan(account, user, queue[0])["action"] == "completed"

    second = packing.scan(account, user, barcode_of(sku))
    assert second["action"] == "posting_selected", second["message"]
    assert second["state"]["active"]["posting_number"] == queue[1], "взято не следующее по очереди"
    assert second["print"]["posting_number"] == queue[1], "стикер следующего не ушёл на печать"


def test_single_candidate_says_nothing_about_others(account, sample_data, user):
    """Когда отправление одно, лишней приписки в сообщении быть не должно."""
    posting = pick_posting(positions=1)
    sku = posting["items"][0]["sku"]
    if len(packing.candidates_for_sku(account, sku, user)) != 1:
        pytest.skip("этот товар нужен не в одном отправлении")
    result = packing.scan(account, user, barcode_of(sku))
    assert "ещё" not in result["message"], result["message"]
