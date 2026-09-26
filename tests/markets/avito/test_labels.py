"""Выгрузка этикеток Avito и замок на сборке.

У Avito этикетка — вход в сборку: её сканируют, чтобы открыть заказ. Значит,
без выгрузки сканировать нечего, и замок опускается так же, как у Ozon.
"""
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.core import accounts, db, labels
from app.main import app
from app.markets.avito import client as avito
from app.markets.avito import pack as avito_pack
from app.markets.avito import sync as avito_sync


def login(client) -> str:
    response = client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/pack"})
    assert response.status_code == 303, response.text
    import re

    page = client.get("/pack")
    return re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1)


def names_in(archive: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        return sorted(zf.namelist())


# -------------------------------------------------------------------- Avito
@pytest.fixture
def avito_cabinet(sample_data):

    cabinet = accounts.get(accounts.create("avito", "Кабинет Avito", "test-client", "test-secret"))
    avito_sync.sync_avito(cabinet)
    return cabinet


def test_avito_orders_awaiting_shipment_are_counted(avito_cabinet):
    account = avito_cabinet
    pending = avito_pack.pending_labels(account["id"])
    assert pending
    statuses = {
        row["status"] for row in db.query(
            f"SELECT status FROM avito_orders WHERE id IN ({','.join('?' for _ in pending)})", pending)
    }
    assert statuses == {avito.STATUS_READY_TO_SHIP}


def test_avito_archive_opens_the_lock(avito_cabinet):
    account = avito_cabinet
    with TestClient(app, follow_redirects=False) as client:
        csrf = login(client)
        assert labels.state(avito_pack.pending_labels(account["id"]))["locked"] is True
        response = client.post(f"/api/pack/labels.zip?shop={account['id']}", headers={"X-CSRF-Token": csrf})
        assert response.status_code == 200, response.text

    assert names_in(response.content), "архив Avito пустой"
    assert labels.state(avito_pack.pending_labels(account["id"])) == {"pending": 0, "locked": False}


def test_the_avito_pack_page_holds_the_gate(avito_cabinet):
    """Замок есть и в сборке Avito — шаблон тот же по смыслу."""
    with TestClient(app, follow_redirects=False) as client:
        login(client)
        # Старый адрес «Сборки» Avito ведёт на общую, с тем же фильтром.
        old = client.get(f"/avito/pack?shop={avito_cabinet['id']}")
        assert old.headers["location"] == f"/pack?shop={avito_cabinet['id']}"
        page = client.get(old.headers["location"])

    assert page.status_code == 200
    assert 'id="label-gate"' in page.text
    assert 'id="scan-panel"' in page.text
    assert 'id="btn-labels"' in page.text
