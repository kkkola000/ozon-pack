"""Разбор ответов Ozon и вычисляемые поля."""
from app import db, store
from app.config import settings


def test_upsert_posting_keeps_local_state(account, sample_data):
    number = db.query_one("SELECT posting_number FROM postings LIMIT 1")["posting_number"]
    db.execute("UPDATE postings SET local_state = 'packed', packed_by = 'ivanov' WHERE posting_number = ?", (number,))

    raw = db.query_one("SELECT raw FROM postings WHERE posting_number = ?", (number,))["raw"]
    import json

    with db.write() as conn:
        store.upsert_posting(conn, account["id"], json.loads(raw))

    row = db.query_one("SELECT local_state, packed_by FROM postings WHERE posting_number = ?", (number,))
    assert row["local_state"] == "packed"
    assert row["packed_by"] == "ivanov"


def test_cancelled_posting_marked_locally(account, sample_data):
    import json

    number = db.query_one("SELECT posting_number FROM postings LIMIT 1")["posting_number"]
    raw = json.loads(db.query_one("SELECT raw FROM postings WHERE posting_number = ?", (number,))["raw"])
    raw["status"] = "cancelled"
    with db.write() as conn:
        store.upsert_posting(conn, account["id"], raw)
    assert db.query_one("SELECT local_state FROM postings WHERE posting_number = ?", (number,))["local_state"] == "cancelled"


def test_urgency_buckets():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    def view(hours):
        return store.posting_view(
            {"account_id": 1, "posting_number": "1-1-1",
             "shipment_date": (now + timedelta(hours=hours)).isoformat(),
             "status": "awaiting_deliver", "local_state": "new", "claim_at": None},
            with_items=False,
        )["urgency"]

    assert view(-2) == "overdue"
    assert view(3) == "urgent"
    assert view(12) == "soon"
    assert view(48) == "ok"


def test_returns_readiness(sample_data):
    """Готов к выдаче — только «В пункте выдачи» (ArrivedAtReturnPlace)."""
    ready = db.query("SELECT status_sys FROM returns WHERE is_ready = 1")
    assert ready, "должны быть возвраты, готовые к выдаче"
    for row in ready:
        assert row["status_sys"] in settings.returns_ready_statuses

    not_ready = db.query("SELECT status_sys FROM returns WHERE is_ready = 0")
    for row in not_ready:
        assert row["status_sys"] not in settings.returns_ready_statuses


def test_returns_loaded_only_in_wanted_statuses(sample_data):
    """Синхронизация забирает нужные статусы, а не всё подряд.

    Нужных два набора: «к выдаче» — список сборщику к поездке, и «получен» —
    из них собирается акт на подтверждение. Всё остальное не сохраняется.
    """
    statuses = {row["status_sys"] for row in db.query("SELECT DISTINCT status_sys FROM returns")}
    assert statuses <= {"ArrivedAtReturnPlace", "ReceivedBySeller"}, statuses


def test_return_leaving_pickup_point_is_dropped(sample_data):
    """Возврат забрали — Ozon его больше не отдаёт, значит из выдачи он уходит."""
    from app import sync

    client = sample_data
    target = client._returns[0]
    assert target["visual"]["status"]["sys_name"] == "ArrivedAtReturnPlace"
    return_id = str(target["id"])
    assert db.query_one("SELECT is_ready FROM returns WHERE id = ?", (return_id,))["is_ready"] == 1

    target["visual"]["status"]["sys_name"] = "ReceivedBySeller"
    target["visual"]["status"]["display_name"] = "Получен продавцом"
    sync.sync_returns()

    assert db.query_one("SELECT is_ready FROM returns WHERE id = ?", (return_id,))["is_ready"] == 0


def test_return_moving_to_seller_leaves_the_pickup_list(sample_data):
    """Возврат уехал к продавцу — из «К выдаче» он уходит при обновлении.

    Раздел строится прямо по статусу строки, а статус панель знает только из
    загрузки: в MovingToSeller возврат не запрашивается вовсе, и в базе
    навсегда оставался прежний «В пункте выдачи». Строка висела в разделе,
    сколько ни жми «Обновить», — сборщик ехал за тем, чего в пункте нет.
    """
    from app import options, sync

    client = sample_data
    target = client._returns[0]
    assert target["visual"]["status"]["sys_name"] == "ArrivedAtReturnPlace"
    return_id = str(target["id"])

    where, params = options.pickup_sql()
    pickup = lambda: [r["id"] for r in db.query(f"SELECT id FROM returns WHERE {where}", params)]
    assert return_id in pickup()
    before = len(pickup())

    target["visual"]["status"]["sys_name"] = "MovingToSeller"
    target["visual"]["status"]["display_name"] = "Едет к продавцу"
    sync.sync_returns()

    assert return_id not in pickup(), "уехавший возврат остался в «К выдаче»"
    assert len(pickup()) == before - 1
    # Остальные на месте: убрали один возврат, а не пересобрали раздел пустым.
    assert before > 1


def test_a_marked_return_keeps_its_row_when_it_leaves_the_pickup(sample_data):
    """Отметку сборщика не стираем вместе со строкой — только снимаем статус."""
    from app import options, sync

    client = sample_data
    target = client._returns[0]
    return_id = str(target["id"])
    db.execute("UPDATE returns SET mark = 'bad', note = 'вскрыта упаковка' WHERE id = ?", (return_id,))

    target["visual"]["status"]["sys_name"] = "MovingToSeller"
    sync.sync_returns()

    row = db.query_one("SELECT status_sys, is_ready, mark, note FROM returns WHERE id = ?", (return_id,))
    assert row is not None, "строку с отметкой удалили"
    assert row["mark"] == "bad" and row["note"] == "вскрыта упаковка"
    assert row["is_ready"] == 0
    where, params = options.pickup_sql()
    assert return_id not in [r["id"] for r in db.query(f"SELECT id FROM returns WHERE {where}", params)]


def test_network_error_does_not_clear_pickup_list(sample_data, monkeypatch):
    """Сбой связи не должен обнулять список готовых к выдаче.

    Обновление пересобирает раздел по ответу площадки, и оборванный ответ
    вычистил бы всё, до чего не дочитали. Поэтому пересборка идёт только после
    полного обхода — этот тест её и сторожит.
    """
    from app import options, sync
    from app.ozon import OzonError

    where, params = options.pickup_sql()
    count = lambda: db.query_one(f"SELECT COUNT(*) c FROM returns WHERE {where}", params)["c"]
    before = count()
    assert before > 0

    def boom(*args, **kwargs):
        raise OzonError("Сеть недоступна")

    monkeypatch.setattr(sample_data, "returns_list", boom)
    sync.sync_returns()

    assert count() == before


def test_products_have_barcodes(sample_data):
    assert db.query_one("SELECT COUNT(*) c FROM product_barcodes")["c"] > 0
    row = db.query_one("SELECT sku, barcodes FROM products WHERE barcodes != '[]' LIMIT 1")
    assert row is not None


def test_ignored_api_filter_still_filters_locally(sample_data, monkeypatch):
    """Если Ozon вернёт всё подряд, лишнее не должно попасть в список выдачи."""
    from app import sync

    client = sample_data
    original = client.returns_list

    def ignores_filter(*, limit=500, last_id=0, filter_=None):
        # Отдаём всё, как будто фильтр по статусу не поддержан
        return original(limit=limit, last_id=last_id, filter_=None)

    monkeypatch.setattr(client, "returns_list", ignores_filter)
    result = sync.sync_returns()

    statuses = {row["status_sys"] for row in db.query("SELECT DISTINCT status_sys FROM returns")}
    assert statuses <= {"ArrivedAtReturnPlace", "ReceivedBySeller"}, statuses
    assert result.get("returns_skipped"), "отброшенные возвраты должны быть посчитаны"
    # Не готов к выдаче здесь только полученный: за ним ехать уже не надо,
    # но отметку по нему поставить ещё предстоит.
    assert db.query_one(
        "SELECT COUNT(*) c FROM returns WHERE is_ready = 0 AND received_at IS NULL"
    )["c"] == 0


def test_returns_in_other_statuses_are_cleaned_up(account, sample_data):
    """Записи, оставшиеся от прежних настроек, удаляются при синхронизации."""
    from app import sync

    db.execute(
        "INSERT INTO returns(account_id, id, type, status_sys, status_name, product_name, quantity, is_ready,"
        " first_seen_at, updated_at)"
        " VALUES(?, 'old-1', 'FBS', 'MovingToSeller', 'Едет к продавцу', 'Старый возврат', 1, 1, ?, ?)",
        (account["id"], db.now_iso(), db.now_iso()),
    )
    sync.sync_returns()
    assert db.query_one("SELECT COUNT(*) c FROM returns WHERE id = 'old-1'")["c"] == 0


def test_statuses_can_be_changed_from_panel(sample_data):
    """Список статусов задаётся в интерфейсе и переопределяет .env."""
    from app import options, sync

    options.set_returns_statuses(["ArrivedAtReturnPlace", "MovingToSeller"])
    assert options.get_returns_statuses() == ["ArrivedAtReturnPlace", "MovingToSeller"]
    sync.sync_returns()

    statuses = {row["status_sys"] for row in db.query("SELECT DISTINCT status_sys FROM returns")}
    assert "MovingToSeller" in statuses


def test_legacy_env_value_is_upgraded(monkeypatch):
    """Старое значение из .env, записанное прежним установщиком, не должно
    возвращать в список выдачи возвраты, которые нельзя забрать."""
    from app import options
    from app.config import settings

    monkeypatch.setattr(settings, "returns_ready_statuses", ["ArrivedAtReturnPlace", "WaitingShipment"])
    assert options.get_returns_statuses() == ["ArrivedAtReturnPlace"]

    # Осознанно выбранное значение уважаем
    monkeypatch.setattr(settings, "returns_ready_statuses", ["WaitingShipment"])
    assert options.get_returns_statuses() == ["WaitingShipment"]

    # Выбор в панели важнее файла
    monkeypatch.setattr(settings, "returns_ready_statuses", ["WaitingShipment"])
    options.set_returns_statuses(["ArrivedAtReturnPlace"])
    assert options.get_returns_statuses() == ["ArrivedAtReturnPlace"]


def test_ozon_print_sheet_shows_pickup_address_without_status(sample_data, account):
    """Лист возвратов Ozon: адрес ПВЗ вместо колонки со статусом."""
    from fastapi.testclient import TestClient

    from app.main import app

    row = db.query_one("SELECT id FROM returns WHERE account_id = ? AND is_ready = 1 LIMIT 1", (account["id"],))
    db.execute(
        "UPDATE returns SET place_name = ?, place_address = ? WHERE account_id = ? AND id = ?",
        ("ПВЗ Москва, Ленинский 25", "Москва, Ленинский пр-т, 25", account["id"], row["id"]),
    )
    with TestClient(app, follow_redirects=False) as client:
        client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/returns"})
        page = client.get("/returns/print")

    assert page.status_code == 200
    assert "Пункт выдачи" in page.text
    assert "Москва, Ленинский пр-т, 25" in page.text
    assert "Статус" not in page.text, "колонку со статусом с листа убрали"
    assert "В пункте выдачи" not in page.text


# ------------------------------------------- когда возврат стал готов к выдаче
def test_arrival_date_is_kept_for_every_return(sample_data):
    """arrived_moment сохраняется по каждому возврату, а не считается на лету."""
    rows = db.query("SELECT id, arrived_at, raw FROM returns WHERE is_ready = 1")
    assert rows, "нет возвратов, готовых к выдаче"
    import json

    for row in rows:
        expected = (json.loads(row["raw"]).get("storage") or {}).get("arrived_moment")
        assert row["arrived_at"], f"у возврата {row['id']} нет даты готовности"
        assert row["arrived_at"][:10] == expected[:10]


def test_arrival_date_is_shown_in_the_card(sample_data, account):
    """Дата готовности видна в строке возврата раздела «К выдаче».

    Нужна, чтобы отличить залежавшийся возврат от привезённого сегодня. Это
    всё, для чего она здесь: список по ней не строится и не сортируется.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    row = db.query_one(
        "SELECT id, arrived_at FROM returns WHERE account_id = ? AND is_ready = 1 "
        "AND arrived_at IS NOT NULL LIMIT 1", (account["id"],)
    )
    with TestClient(app, follow_redirects=False) as client:
        client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/returns"})
        page = client.get("/returns")

    assert page.status_code == 200
    assert f"Готов к выдаче с {store.local_time(row['arrived_at'], '%d.%m.%Y')}" in page.text


def test_arrival_date_does_not_decide_the_section(sample_data, account):
    """По дате готовности возврат никуда не попадает и ниоткуда не выпадает.

    Состав раздела решают отмеченные в настройках статусы. Дата готовности —
    только для сведения: ставим её старее некуда и убеждаемся, что список тот же.
    """
    from app import options

    before = [row["id"] for row in db.query(
        "SELECT id FROM returns WHERE account_id = ? AND is_ready = 1 ORDER BY id", (account["id"],)
    )]
    assert before
    db.execute("UPDATE returns SET arrived_at = ? WHERE account_id = ?", ("2019-01-01T00:00:00+00:00", account["id"]))

    where, params = options.pickup_sql()
    after = [row["id"] for row in db.query(
        f"SELECT id FROM returns WHERE account_id = ? AND {where} ORDER BY id", [account["id"]] + params
    )]
    assert after == before
