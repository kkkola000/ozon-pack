"""Разбор ответов Ozon и вычисляемые поля."""
from app import db, store
from app.config import settings


def test_upsert_posting_keeps_local_state(demo_data):
    number = db.query_one("SELECT posting_number FROM postings LIMIT 1")["posting_number"]
    db.execute("UPDATE postings SET local_state = 'packed', packed_by = 'ivanov' WHERE posting_number = ?", (number,))

    raw = db.query_one("SELECT raw FROM postings WHERE posting_number = ?", (number,))["raw"]
    import json

    with db.write() as conn:
        store.upsert_posting(conn, json.loads(raw))

    row = db.query_one("SELECT local_state, packed_by FROM postings WHERE posting_number = ?", (number,))
    assert row["local_state"] == "packed"
    assert row["packed_by"] == "ivanov"


def test_cancelled_posting_marked_locally(demo_data):
    import json

    number = db.query_one("SELECT posting_number FROM postings LIMIT 1")["posting_number"]
    raw = json.loads(db.query_one("SELECT raw FROM postings WHERE posting_number = ?", (number,))["raw"])
    raw["status"] = "cancelled"
    with db.write() as conn:
        store.upsert_posting(conn, raw)
    assert db.query_one("SELECT local_state FROM postings WHERE posting_number = ?", (number,))["local_state"] == "cancelled"


def test_urgency_buckets():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    def view(hours):
        return store.posting_view(
            {"posting_number": "1-1-1", "shipment_date": (now + timedelta(hours=hours)).isoformat(),
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
