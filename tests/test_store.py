"""Разбор ответов Ozon и вычисляемые поля."""
from app import db, store
from app.config import settings


def test_upsert_posting_keeps_local_state(account, demo_data):
    number = db.query_one("SELECT posting_number FROM postings LIMIT 1")["posting_number"]
    db.execute("UPDATE postings SET local_state = 'packed', packed_by = 'ivanov' WHERE posting_number = ?", (number,))

    raw = db.query_one("SELECT raw FROM postings WHERE posting_number = ?", (number,))["raw"]
    import json

    with db.write() as conn:
        store.upsert_posting(conn, account["id"], json.loads(raw))

    row = db.query_one("SELECT local_state, packed_by FROM postings WHERE posting_number = ?", (number,))
    assert row["local_state"] == "packed"
    assert row["packed_by"] == "ivanov"


def test_cancelled_posting_marked_locally(account, demo_data):
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


def test_returns_readiness(demo_data):
    """Готов к выдаче — только «В пункте выдачи» (ArrivedAtReturnPlace)."""
    ready = db.query("SELECT status_sys FROM returns WHERE is_ready = 1")
    assert ready, "должны быть возвраты, готовые к выдаче"
    for row in ready:
        assert row["status_sys"] in settings.returns_ready_statuses

    not_ready = db.query("SELECT status_sys FROM returns WHERE is_ready = 0")
    for row in not_ready:
        assert row["status_sys"] not in settings.returns_ready_statuses


def test_returns_loaded_only_in_wanted_status(demo_data):
    """Синхронизация забирает у Ozon именно нужный статус, а не всё подряд."""
    statuses = {row["status_sys"] for row in db.query("SELECT DISTINCT status_sys FROM returns")}
    assert statuses == {"ArrivedAtReturnPlace"}, statuses


def test_return_leaving_pickup_point_is_dropped(demo_data):
    """Возврат забрали — Ozon его больше не отдаёт, значит из выдачи он уходит."""
    from app import sync

    client = demo_data
    target = client._returns[0]
    assert target["visual"]["status"]["sys_name"] == "ArrivedAtReturnPlace"
    return_id = str(target["id"])
    assert db.query_one("SELECT is_ready FROM returns WHERE id = ?", (return_id,))["is_ready"] == 1

    target["visual"]["status"]["sys_name"] = "ReceivedBySeller"
    target["visual"]["status"]["display_name"] = "Получен продавцом"
    sync.sync_returns()

    assert db.query_one("SELECT is_ready FROM returns WHERE id = ?", (return_id,))["is_ready"] == 0


def test_network_error_does_not_clear_pickup_list(demo_data, monkeypatch):
    """Сбой связи не должен обнулять список готовых к выдаче."""
    from app import sync
    from app.ozon import OzonError

    before = db.query_one("SELECT COUNT(*) c FROM returns WHERE is_ready = 1")["c"]
    assert before > 0

    def boom(*args, **kwargs):
        raise OzonError("Сеть недоступна")

    monkeypatch.setattr(demo_data, "returns_list", boom)
    sync.sync_returns()

    assert db.query_one("SELECT COUNT(*) c FROM returns WHERE is_ready = 1")["c"] == before


def test_products_have_barcodes(demo_data):
    assert db.query_one("SELECT COUNT(*) c FROM product_barcodes")["c"] > 0
    row = db.query_one("SELECT sku, barcodes FROM products WHERE barcodes != '[]' LIMIT 1")
    assert row is not None


def test_ignored_api_filter_still_filters_locally(demo_data, monkeypatch):
    """Если Ozon вернёт всё подряд, лишнее не должно попасть в список выдачи."""
    from app import sync

    client = demo_data
    original = client.returns_list

    def ignores_filter(*, limit=500, last_id=0, filter_=None):
        # Отдаём всё, как будто фильтр по статусу не поддержан
        return original(limit=limit, last_id=last_id, filter_=None)

    monkeypatch.setattr(client, "returns_list", ignores_filter)
    result = sync.sync_returns()

    statuses = {row["status_sys"] for row in db.query("SELECT DISTINCT status_sys FROM returns")}
    assert statuses == {"ArrivedAtReturnPlace"}, statuses
    assert result.get("returns_skipped"), "отброшенные возвраты должны быть посчитаны"
    assert db.query_one("SELECT COUNT(*) c FROM returns WHERE is_ready = 0")["c"] == 0


def test_returns_in_other_statuses_are_cleaned_up(account, demo_data):
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


def test_taken_returns_survive_cleanup(account, demo_data):
    """Забранные возвраты остаются в истории, даже если статус уже другой."""
    from app import sync

    db.execute(
        "INSERT INTO returns(account_id, id, type, status_sys, status_name, product_name, quantity, is_ready,"
        " taken_at, taken_by, first_seen_at, updated_at)"
        " VALUES(?, 'taken-1', 'FBS', 'ReceivedBySeller', 'Получен продавцом', 'Забранный', 1, 0, ?, 'admin', ?, ?)",
        (account["id"], db.now_iso(), db.now_iso(), db.now_iso()),
    )
    sync.sync_returns()
    assert db.query_one("SELECT COUNT(*) c FROM returns WHERE id = 'taken-1'")["c"] == 1


def test_statuses_can_be_changed_from_panel(demo_data):
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
