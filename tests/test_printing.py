"""Печать на принтер этикеток через локального агента."""
import pytest

from app import db, options, packing, printing
from app.config import settings
from app.tspl import PrinterConfig, build_label_job, build_test_job, pack_bitmap
from tests.conftest import pick_posting


@pytest.fixture
def printer_on(demo_data):
    options.set_printer_config({"enabled": True, "host": "192.168.1.50", "port": 9100})
    return options.get_printer_config()


def test_disabled_by_default(demo_data):
    assert not printing.is_enabled(), "без настройки печать через агента не должна включаться"


def test_enabled_needs_host(demo_data):
    options.set_printer_config({"enabled": True, "host": ""})
    assert not printing.is_enabled(), "без адреса принтера печатать некуда"


def test_queue_lifecycle(printer_on, user):
    posting = pick_posting()
    result = printing.enqueue_label(user, [posting["posting_number"]])
    assert result["bytes"] > 1000

    assert printing.status()["queued"] == 1
    job = printing.next_job()
    assert job and job["id"] == result["job_id"]
    assert db.query_one("SELECT status FROM print_jobs WHERE id = ?", (job["id"],))["status"] == "sent"
    assert printing.next_job() is None, "одно задание не должно уйти двум агентам"

    printing.ack(job["id"], True)
    row = db.query_one("SELECT status, done_at FROM print_jobs WHERE id = ?", (job["id"],))
    assert row["status"] == "done" and row["done_at"]


def test_failed_job_returns_to_queue_then_gives_up(printer_on, user):
    printing.enqueue_test(user)
    for attempt in range(1, printing.MAX_ATTEMPTS + 1):
        job = printing.next_job()
        assert job, f"попытка {attempt}: задание должно вернуться в очередь"
        printing.ack(job["id"], False, "принтер не отвечает")
    assert printing.next_job() is None
    row = db.query_one("SELECT status, error FROM print_jobs ORDER BY id DESC LIMIT 1")
    assert row["status"] == "failed" and "не отвечает" in row["error"]


def test_label_job_is_valid_tspl(printer_on, user):
    posting = pick_posting()
    printing.enqueue_label(user, [posting["posting_number"]])
    payload = printing.next_job()["payload"]

    header = payload[: payload.index(b"BITMAP")].decode("ascii")
    assert "SIZE 75 mm,120 mm" in header
    assert "GAP 2 mm,0 mm" in header
    assert header.rstrip().endswith("CLS")
    assert payload.rstrip().endswith(b"PRINT 1,1")

    import re

    match = re.search(rb"BITMAP (\d+),(\d+),(\d+),(\d+),(\d+),", payload)
    width_bytes, height = int(match.group(3)), int(match.group(4))
    assert width_bytes * 8 == 600 and height == 960, "этикетка 75x120 мм при 203 dpi"
    data = payload[match.end() : match.end() + width_bytes * height]
    assert len(data) == width_bytes * height
    assert any(byte != 0xFF for byte in data), "на этикетке должны быть чёрные точки"


def test_copies_and_direction_reach_the_printer(demo_data, user):
    options.set_printer_config({"enabled": True, "host": "192.168.1.50", "copies": 3, "direction": 0})
    printing.enqueue_test(user)
    payload = printing.next_job()["payload"].decode("ascii")
    assert "DIRECTION 0,0" in payload
    assert build_test_job(printing.printer_config()).decode("ascii").count("PRINT 1,1") == 1


def test_scan_sends_label_to_printer_instead_of_browser(printer_on, user, monkeypatch):
    """При включённом принтере окно печати в браузере не открывается."""
    monkeypatch.setattr(settings, "autoprint", True)
    posting = pick_posting(positions=1)
    result = packing.select_posting(user, posting["posting_number"])

    assert result["print"] is None, "браузеру печатать нечего"
    assert result["queued_print"]["job_id"], "стикер должен уйти в очередь принтера"
    assert printing.status()["queued"] == 1


def test_token_required(printer_on):
    assert printing.check_token("") is False
    assert printing.check_token("не тот ключ") is False
    assert printing.check_token(options.get_agent_token()) is True


def test_bitmap_packing_matches_tspl_convention():
    """В TSPL единица — белая точка, ноль — чёрная."""
    black = bytes([0] * 8)
    white = bytes([255] * 8)
    packed_black, _ = pack_bitmap(black, 8, 1, threshold=128)
    packed_white, _ = pack_bitmap(white, 8, 1, threshold=128)
    assert packed_black == b"\x00" and packed_white == b"\xff"


def test_label_wider_than_printer_is_rejected():
    config = PrinterConfig(host="192.168.1.50")
    too_wide = config.max_width_dots + 8
    with pytest.raises(ValueError, match="больше печатного поля"):
        build_label_job([(bytes([255] * too_wide), too_wide, 1)], config, width_mm=200, height_mm=120)


def test_cleanup_removes_old_jobs(printer_on, user):
    printing.enqueue_test(user)
    job = printing.next_job()
    printing.ack(job["id"], True)
    db.execute("UPDATE print_jobs SET created_at = datetime('now', '-30 days') WHERE id = ?", (job["id"],))
    assert printing.cleanup(days=7) == 1
    assert db.query_one("SELECT COUNT(*) c FROM print_jobs")["c"] == 0


def test_bad_token_is_rejected_over_http(printer_on):
    """Чужой ключ должен получать отказ, а не ошибку сервера."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, follow_redirects=False) as client:
        for token in ("", "чужой ключ", "x" * 64):
            assert client.get("/api/print/next", params={"token": token}).status_code == 401
        assert client.post("/api/print/ack", json={"token": "нет", "job_id": 1}).status_code == 401

        good = options.get_agent_token()
        assert client.get("/api/print/next", params={"token": good}).status_code == 204
        config = client.get("/api/print/config", params={"token": good})
        assert config.status_code == 200
        assert config.json()["printer"]["host"] == "192.168.1.50"


def test_agent_receives_job_over_http(printer_on, user):
    from fastapi.testclient import TestClient

    from app.main import app

    printing.enqueue_test(user)
    with TestClient(app, follow_redirects=False) as client:
        token = options.get_agent_token()
        response = client.get("/api/print/next", params={"token": token})
        assert response.status_code == 200
        assert response.content.startswith(b"SIZE ")
        job_id = int(response.headers["X-Job-Id"])

        ack = client.post("/api/print/ack", json={"token": token, "job_id": job_id, "ok": True})
        assert ack.status_code == 200
        assert db.query_one("SELECT status FROM print_jobs WHERE id = ?", (job_id,))["status"] == "done"
