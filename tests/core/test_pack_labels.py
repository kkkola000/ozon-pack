"""Скачивание наклеек больше не блокирует сборку; прежний ZIP совместим."""
import io
import re
import zipfile
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.core import accounts
from app.main import app
from app.markets.avito import pack as avito_pack
from app.markets.avito import sync as avito_sync
from app.markets.ozon import pack as ozon_pack
from app.markets.yandex import client as yandex_client
from app.markets.yandex import pack as yandex_pack
from app.markets.yandex import sync as yandex_sync


@pytest.fixture
def warehouse(sample_data):
    """Склад с тремя кабинетами: Ozon по умолчанию, Avito и Маркет."""
    avito = accounts.get(accounts.create("avito", "Кабинет Avito", "test-client", "test-secret"))
    avito_sync.sync_avito(avito)
    yandex = accounts.get(accounts.create("yandex", "Маркет", "9000001", "test-token"))
    yandex_sync.sync_yandex(yandex)
    return {"ozon": accounts.default_account(), "avito": avito, "yandex": yandex}


@pytest.fixture
def client(warehouse):
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/pack"})
        page = test_client.get("/pack")
        test_client.headers["X-CSRF-Token"] = re.search(
            r'name="csrf-token" content="([^"]*)"', page.text).group(1)
        yield test_client


def pending(warehouse) -> dict[str, list[str]]:
    return {
        "ozon": ozon_pack.pending_labels(warehouse["ozon"]["id"]),
        "avito": avito_pack.pending_labels(warehouse["avito"]["id"]),
        "yandex": yandex_pack.pending_labels(warehouse["yandex"]["id"]),
    }


def names_in(archive: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        return sorted(zf.namelist())


# ------------------------------------------------------------- какие заказы
def test_labels_wait_for_exactly_the_orders_awaiting_shipment(warehouse):
    """Новые наклейки на «Сборке» — ровно заказы «Ожидает отгрузки» из «Заказов».

    У всех площадок одно правило: не «Ожидает сборки» (Маркет отдал бы и такой
    ярлык, но собирать рано) и не «Собран».
    """
    from app.core import board as core_board

    waiting = pending(warehouse)
    for code, keys in waiting.items():
        expected = {str(card["id"]) for card in core_board.rows([warehouse[code]], "deliver")}
        assert set(keys) == expected, code
        early = {str(card["id"]) for card in core_board.rows([warehouse[code]], "packaging")}
        assert not set(keys) & early, f"{code}: «Ожидает сборки» в выгрузке"


def test_packed_order_does_not_hold_the_lock(warehouse, user):
    """Собранный заказ закрыли сканом наклейки — она уже на коробке, замок его не держит."""
    ozon = warehouse["ozon"]
    number = ozon_pack.pending_labels(ozon["id"])[0]
    ozon_pack.complete(ozon, user, number)
    assert number not in ozon_pack.pending_labels(ozon["id"])


# ---------------------------------------------------------------------- замок
def test_pending_labels_never_lock_the_workspace(client, warehouse):
    assert all(pending(warehouse).values())
    for picked in ['all', *(str(shop['id']) for shop in warehouse.values())]:
        result = client.get(f'/api/pack/state?shop={picked}').json()
        assert 'labels' not in result
        assert result['state']['active'] is None



def test_packing_page_keeps_the_scanner_without_a_download_gate(client, warehouse):
    page = client.get('/pack').text
    assert 'id="label-gate"' not in page
    assert 'id="btn-labels"' not in page
    assert 'id="scan-panel"' in page
    assert '/static/sticker_downloads.js' in page



def test_optional_zip_only_lists_cabinets_with_pending_files(client, warehouse):
    from app.core import labels
    assert client.post(f"/api/pack/labels.zip?shop={warehouse['avito']['id']}").status_code == 200
    remaining = labels.pending_everywhere(list(warehouse.values()))
    assert {item['account']['id'] for item in remaining} == {warehouse['ozon']['id'], warehouse['yandex']['id']}



# ---------------------------------------------------------------------- архив
def test_one_archive_holds_the_labels_of_every_cabinet(client, warehouse):
    """Одно нажатие — один архив на все магазины, папка на кабинет."""
    waiting = pending(warehouse)
    response = client.post("/api/pack/labels.zip")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"

    names = names_in(response.content)
    folders = {name.split("/")[0] for name in names}
    assert folders == {"Ozon", "Кабинет Avito", "Маркет"}
    assert len(names) == sum(len(keys) for keys in waiting.values())
    assert response.headers["X-Labels-Saved"] == str(len(names))


def test_the_archive_opens_the_lock_for_everyone(client, warehouse):
    """После выгрузки пустеет очередь каждого кабинета, а не только текущего."""
    assert client.post("/api/pack/labels.zip").status_code == 200

    assert pending(warehouse) == {"ozon": [], "avito": [], "yandex": []}
    assert "labels" not in client.get("/api/pack/state").json()


def test_nothing_left_to_download_is_refused(client, warehouse):
    client.post("/api/pack/labels.zip")
    again = client.post("/api/pack/labels.zip")
    assert again.status_code == 400
    assert "уже выгружены" in again.json()["detail"]


def test_the_archive_needs_csrf(client, warehouse):
    assert client.post("/api/pack/labels.zip", headers={"X-CSRF-Token": ""}).status_code == 403


# --------------------------------------------------------------------- фильтр
def test_filter_keeps_pending_files_separate_without_locking(client, warehouse):
    from app.core import labels
    remaining = labels.pending_everywhere([warehouse['yandex']])
    assert [item['account']['id'] for item in remaining] == [warehouse['yandex']['id']]
    assert remaining[0]['keys'] == pending(warehouse)['yandex']
    assert 'labels' not in client.get(f"/api/pack/state?shop={warehouse['yandex']['id']}").json()



def test_the_filter_narrows_the_archive(client, warehouse):
    """Под фильтром качаются наклейки выбранного кабинета — остальные ждут."""
    before = pending(warehouse)
    response = client.post(f"/api/pack/labels.zip?shop={warehouse['yandex']['id']}")
    assert response.status_code == 200, response.text

    # Одна площадка — папки не нужно: лишний клик там ничего не объясняет.
    assert names_in(response.content) == sorted(f"{key}.pdf" for key in before["yandex"])
    after = pending(warehouse)
    assert after["yandex"] == []
    assert after["ozon"] == before["ozon"] and after["avito"] == before["avito"]


# ------------------------------------------------------------------- отказы
def break_yandex(monkeypatch, cabinet) -> None:
    """Маркет перестаёт отдавать ярлыки — как при его очередной поломке."""
    def refuse(*_args, **_kwargs):
        raise RuntimeError("Маркет прилёг")

    monkeypatch.setattr(yandex_client.get_client(cabinet), "order_labels", refuse)


def test_a_refusing_marketplace_does_not_stop_the_others(client, warehouse, monkeypatch):
    """Маркет отказал — стикеры Ozon и этикетки Avito всё равно уезжают.

    Иначе одна сломанная площадка оставляла бы сборщика вообще без наклеек, а
    смена не начиналась бы вовсе.
    """
    break_yandex(monkeypatch, warehouse["yandex"])
    before = pending(warehouse)

    response = client.post("/api/pack/labels.zip")
    assert response.status_code == 200, response.text
    assert unquote(response.headers["X-Labels-Refused"]) == "Маркет"

    after = pending(warehouse)
    assert after["ozon"] == [] and after["avito"] == []
    assert after["yandex"] == before["yandex"], "невыгруженное должно остаться в очереди"
    # Замок не открылся: ярлыков Маркета так и нет.
    assert "labels" not in client.get("/api/pack/state").json()


def test_everyone_refusing_is_an_error(client, warehouse, monkeypatch):
    """Ни одной наклейки — это отказ, а не пустой архив в браузере."""
    break_yandex(monkeypatch, warehouse["yandex"])

    response = client.post(f"/api/pack/labels.zip?shop={warehouse['yandex']['id']}")
    assert response.status_code == 502
    assert "Маркет" in response.json()["detail"]


# ------------------------------------------------------------- скачано ≠ напечатано
def test_the_archive_marks_downloaded_not_printed(client, warehouse):
    """Архив ставит «скачано» — по нему открывается замок. «Печатался» он не ставит.

    Раньше у Ozon выгрузка засчитывалась и как печать, а у Avito и Маркета —
    нет. Теперь одинаково: печать отмечает только печать.
    """
    from app.core import db

    response = client.post("/api/pack/labels.zip")
    assert response.status_code == 200, response.text
    for table in ("postings", "avito_orders", "yandex_orders"):
        printed = db.query_one(f"SELECT COUNT(*) AS c FROM {table} WHERE print_count > 0")["c"]
        assert printed == 0, f"{table}: выгрузка засчиталась как печать"
    assert not any(pending(warehouse).values())

    # Новый заказ без отметки «скачано» — замок снова закрыт.
    number = db.query_one("SELECT posting_number FROM postings WHERE label_saved_at IS NOT NULL LIMIT 1")[0]
    db.execute("UPDATE postings SET label_saved_at = NULL WHERE posting_number = ?", (number,))
    assert pending(warehouse)["ozon"] == [number]
    assert "labels" not in client.get("/api/pack/state").json()


# ------------------------------------------------------------- бронь при сквозной сборке
def test_opening_an_ozon_posting_frees_a_held_avito_order(warehouse, user):
    """Держал заказ Avito, открыл отправление Ozon — бронь Avito снята.

    Раньше Ozon снимал бронь только в своей таблице: заказ Avito висел
    забронированным до истечения CLAIM_TTL_MINUTES.
    """
    from app.core import db, pack_state
    from app.markets.ozon import store as ozon_store

    avito = warehouse["avito"]
    held = db.query_one("SELECT id FROM avito_orders WHERE account_id = ? AND status = 'ready_to_ship' LIMIT 1",
                        (avito["id"],))["id"]
    with db.write() as conn:
        pack_state.save(conn, avito, user, held, [])
    db.execute("UPDATE avito_orders SET claim_user_id = ?, claim_login = ?, claim_at = ? WHERE id = ?",
               (user["id"], user["login"], db.now_iso(), held))

    number = db.query_one("SELECT posting_number FROM postings WHERE status = ? AND local_state = 'new' LIMIT 1",
                          (ozon_store.STATUS_AWAITING_DELIVER,))[0]
    ozon_pack.select_posting(warehouse["ozon"], user, number)
    assert db.query_one("SELECT claim_login FROM avito_orders WHERE id = ?", (held,))["claim_login"] is None
    assert db.query_one("SELECT account_id, posting_number FROM pack_state WHERE user_id = ?",
                        (user["id"],))["posting_number"] == number


# ------------------------------------------------- переход в «Ожидает отгрузки»
def test_label_saved_before_deliver_does_not_count(warehouse):
    """Наклейка считается, только если скачана после того, как заказ стал «Ожидает отгрузки»."""
    from app.core import db, labels
    from app.markets.ozon import store as ozon_store

    ozon = warehouse["ozon"]
    number = ozon_pack.pending_labels(ozon["id"])[0]
    labels.note_deliver(ozon)
    db.execute("UPDATE postings SET label_saved_at = '2026-01-01T00:00:00+00:00' WHERE posting_number = ?", (number,))
    assert number in ozon_pack.pending_labels(ozon["id"]), "скачана раньше перехода — не в счёт"

    labels.mark_saved("postings", ozon["id"], [number], "posting_number")
    assert number not in ozon_pack.pending_labels(ozon["id"])

    # Вернулся в «Ожидает сборки» и снова пришёл в «Ожидает отгрузки» — нужна новая.
    db.execute("UPDATE postings SET status = ? WHERE posting_number = ?",
               (ozon_store.STATUS_AWAITING_PACKAGING, number))
    labels.note_deliver(ozon)
    assert db.query_one("SELECT deliver_since FROM postings WHERE posting_number = ?", (number,))["deliver_since"] is None
    db.execute("UPDATE postings SET status = ?, label_saved_at = '2026-01-01T00:00:00+00:00' "
               "WHERE posting_number = ?", (ozon_store.STATUS_AWAITING_DELIVER, number))
    labels.note_deliver(ozon)
    assert number in ozon_pack.pending_labels(ozon["id"])


def test_sync_notes_the_moment_for_every_market(warehouse):
    """Отметку ставит обычная синхронизация — у всех площадок одинаково."""
    from app.core import db, sync

    for code, table in (("ozon", "postings"), ("avito", "avito_orders"), ("yandex", "yandex_orders")):
        sync.sync_account(warehouse[code])
        waiting = pending(warehouse)[code]
        assert waiting, code
        rows = db.query(f"SELECT deliver_since FROM {table} WHERE account_id = ? AND label_saved_at IS NULL",
                        (warehouse[code]["id"],))
        assert any(row["deliver_since"] for row in rows), f"{code}: синхронизация не отметила переход"


def test_orders_already_waiting_keep_their_labels_after_update(warehouse):
    """Первый запуск новой версии: скачанное до неё заново качать не нужно."""
    from app.core import db, labels

    ozon = warehouse["ozon"]
    saved = ozon_pack.pending_labels(ozon["id"])[:2]
    labels.mark_saved("postings", ozon["id"], saved, "posting_number")
    db.execute("UPDATE postings SET deliver_since = NULL")
    db.execute("DELETE FROM kv WHERE key = ?", (labels.KV_DELIVER_NOTED,))
    with db.write() as conn:
        labels.note_deliver_once(conn)
    after = ozon_pack.pending_labels(ozon["id"])
    assert not set(saved) & set(after), "скачанные до обновления остаются скачанными"
    assert after, "нескачанные по-прежнему ждут"


# ------------------------------------------------- замок держит и сервер
def test_server_opens_an_order_before_any_bulk_download(client, warehouse):
    from tests.conftest import pick_posting
    posting = pick_posting()
    opened = client.post('/api/pack/scan', json={'code': posting['posting_number']}).json()
    assert opened['action'] == 'posting_selected', opened
    assert opened['state']['active']
    assert opened['state']['sticker_download']['ready'] is True
    assert 'labels' not in opened



def test_new_pending_order_never_interrupts_current_or_next_packing(client, warehouse):
    from app.core import db
    from tests.conftest import pick_posting
    posting = pick_posting()
    opened = client.post('/api/pack/scan', json={'code': posting['posting_number']}).json()
    assert opened['action'] == 'posting_selected', opened
    db.execute('UPDATE postings SET label_saved_at = NULL')
    assert 'labels' not in client.get('/api/pack/state').json()
    released = client.post('/api/pack/release').json()
    assert released['status'] == 'ok'
    assert 'labels' not in released
    again = client.post('/api/pack/scan', json={'code': posting['posting_number']}).json()
    assert again['action'] == 'posting_selected', again
