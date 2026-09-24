"""Выгрузка стикеров Ozon на компьютер и замок на сборке.

Стикер Ozon отдаёт, пока отправление ждёт отгрузки; после отгрузки его уже не
взять. Поэтому выгрузка идёт первой, до сканирования: не забрали вовремя —
стикер пропал навсегда. Сам файл панель не хранит, на диске сервера его нет.
"""
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.core import accounts, db, labels
from app.main import app
from app.markets.ozon import pack as ozon_pack
from app.markets.ozon import store as ozon_store


@pytest.fixture
def client(sample_data):
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def login(client) -> str:
    response = client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/pack"})
    assert response.status_code == 303, response.text
    import re

    page = client.get("/pack")
    return re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1)


def names_in(archive: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        return sorted(zf.namelist())


# ------------------------------------------------------------------- отбор
def test_only_postings_awaiting_shipment_are_counted(sample_data):
    """Считаем «Ожидает отгрузки»: там стикер есть и его ещё можно взять."""
    account = accounts.default_account()
    pending = ozon_pack.pending_labels(account["id"])
    assert pending, "нет отправлений, ждущих выгрузки"

    statuses = {
        row["status"] for row in db.query(
            f"SELECT status FROM postings WHERE posting_number IN ({','.join('?' for _ in pending)})",
            pending)
    }
    assert statuses == {ozon_store.STATUS_AWAITING_DELIVER}


def test_a_shipped_posting_does_not_hold_the_lock(sample_data):
    """Отгруженное замок не держит: его стикер уже не получить.

    Иначе одно такое отправление заперло бы склад навсегда — кнопка висит, а
    выгрузить по ней нечего.
    """
    account = accounts.default_account()
    target = ozon_pack.pending_labels(account["id"])[0]
    db.execute("UPDATE postings SET status = 'delivering' WHERE posting_number = ?", (target,))

    assert target not in ozon_pack.pending_labels(account["id"])


def test_a_saved_posting_leaves_the_queue(sample_data):
    """Стикер выгружен — отправление из очереди уходит, второй раз не тянем."""
    account = accounts.default_account()
    before = ozon_pack.pending_labels(account["id"])
    labels.mark_saved("postings", account["id"], before[:1], "posting_number")

    after = ozon_pack.pending_labels(account["id"])
    assert before[0] not in after
    assert len(after) == len(before) - 1


def test_the_lock_opens_when_nothing_is_left(sample_data):
    account = accounts.default_account()
    assert labels.state(ozon_pack.pending_labels(account["id"]))["locked"] is True
    labels.mark_saved("postings", account["id"], ozon_pack.pending_labels(account["id"]), "posting_number")

    state = labels.state(ozon_pack.pending_labels(account["id"]))
    assert state == {"pending": 0, "locked": False}


# ------------------------------------------------------------------- архив
def test_archive_has_a_file_per_posting(client):
    """В архиве по файлу на отправление — иначе нужный стикер потом не найти."""
    csrf = login(client)
    account = accounts.default_account()
    expected = ozon_pack.pending_labels(account["id"])

    response = client.post("/api/pack/labels.zip", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert ".zip" in response.headers["content-disposition"]
    assert names_in(response.content) == sorted(f"{n}.pdf" for n in expected)


def test_archive_marks_what_it_took(client):
    """После выгрузки сборка открывается, а очередь пустеет."""
    csrf = login(client)
    account = accounts.default_account()
    assert client.post("/api/pack/labels.zip", headers={"X-CSRF-Token": csrf}).status_code == 200

    assert ozon_pack.pending_labels(account["id"]) == []
    assert labels.state(ozon_pack.pending_labels(account["id"]))["locked"] is False
    saved = db.query_one(
        "SELECT COUNT(*) AS c FROM postings WHERE account_id = ? AND label_saved_at IS NOT NULL",
        (account["id"],))["c"]
    assert saved > 0


def test_nothing_to_download_is_refused(client):
    csrf = login(client)
    client.post("/api/pack/labels.zip", headers={"X-CSRF-Token": csrf})
    again = client.post("/api/pack/labels.zip", headers={"X-CSRF-Token": csrf})
    assert again.status_code == 400
    assert "уже выгружены" in again.json()["detail"]


def test_archive_needs_csrf(client):
    login(client)
    assert client.post("/api/pack/labels.zip").status_code == 403


def test_the_download_is_written_to_the_log(client):
    csrf = login(client)
    client.post("/api/pack/labels.zip", headers={"X-CSRF-Token": csrf})
    row = db.query_one("SELECT message FROM events WHERE kind = 'labels_archive' ORDER BY id DESC LIMIT 1")
    assert row and "Выгружены стикеры" in row["message"]


def test_nothing_is_stored_on_disk(client, tmp_path):
    """Панель стикеры у себя не держит: файл уезжает в браузер и живёт там."""
    from app.core.config import settings

    csrf = login(client)
    data_dir = __import__("pathlib").Path(settings.db_path).parent
    before = {p.name for p in data_dir.iterdir()} if data_dir.exists() else set()

    assert client.post("/api/pack/labels.zip", headers={"X-CSRF-Token": csrf}).status_code == 200

    after = {p.name for p in data_dir.iterdir()} if data_dir.exists() else set()
    assert not {name for name in after - before if name.endswith((".pdf", ".zip"))}


# --------------------------------------------------------------- состояние
def test_state_carries_the_lock(client):
    csrf = login(client)
    state = client.get("/api/pack/state").json()
    assert state["labels"]["locked"] is True
    assert state["labels"]["pending"] > 0

    client.post("/api/pack/labels.zip", headers={"X-CSRF-Token": csrf})
    opened = client.get("/api/pack/state").json()["labels"]
    assert opened["pending"] == 0 and opened["locked"] is False and opened["shops"] == []


def test_the_pack_page_holds_the_gate(client):
    """На странице сборки есть и замок, и поле — показывает их уже JS."""
    login(client)
    page = client.get("/pack")
    assert page.status_code == 200
    assert 'id="label-gate"' in page.text
    assert 'id="scan-panel"' in page.text
    assert 'id="btn-labels"' in page.text
