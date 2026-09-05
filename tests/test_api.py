"""Проверки HTTP-слоя: доступ, CSRF, основные страницы."""
import re

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture
def client(demo_data):
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def login(client) -> str:
    response = client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/pack"})
    assert response.status_code == 303, response.text
    page = client.get("/pack")
    return re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1)


def test_anonymous_redirected(client):
    assert client.get("/pack").status_code == 303
    assert client.get("/api/state").status_code == 401


def test_healthz_is_public(client):
    assert client.get("/healthz").json()["status"] == "ok"


def test_login_and_pages(client):
    csrf = login(client)
    assert csrf
    for url in ("/pack", "/orders?tab=packaging", "/orders?tab=deliver", "/returns", "/returns/print", "/logs", "/settings"):
        assert client.get(url).status_code == 200, url


def test_csrf_required(client):
    login(client)
    assert client.post("/api/scan", json={"code": "1"}).status_code == 403


def test_scan_endpoint(client):
    csrf = login(client)
    row = db.query_one(
        """SELECT pb.barcode FROM product_barcodes pb
           JOIN posting_items i ON i.sku = pb.sku AND i.account_id = pb.account_id
           JOIN postings p ON p.posting_number = i.posting_number AND p.account_id = i.account_id
           WHERE p.status = 'awaiting_deliver' LIMIT 1"""
    )
    response = client.post("/api/scan", json={"code": row["barcode"]}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert response.json()["action"] in ("posting_selected", "need_choice")


def test_packer_cannot_open_settings(client):
    from app.security import hash_password

    db.execute(
        "INSERT INTO users(login, password_hash, role, active, created_at) VALUES('packer1', ?, 'packer', 1, ?)",
        (hash_password("packer123"), db.now_iso()),
    )
    client.post("/login", data={"login": "packer1", "password": "packer123", "next": "/pack"})
    assert client.get("/settings").status_code == 403
    assert client.get("/pack").status_code == 200


def test_returns_taken_flow(client):
    csrf = login(client)
    return_id = db.query_one("SELECT id FROM returns WHERE is_ready = 1 LIMIT 1")["id"]
    response = client.post("/api/returns/taken", json={"ids": [return_id], "taken": True}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert db.query_one("SELECT taken_at FROM returns WHERE id = ?", (return_id,))["taken_at"]


def test_label_pdf(client):
    login(client)
    number = db.query_one("SELECT posting_number FROM postings WHERE status = 'awaiting_deliver' LIMIT 1")["posting_number"]
    response = client.get(f"/api/label/{number}.pdf")
    assert response.status_code == 200
    assert response.content[:4] == b"%PDF"


def test_cabinet_api_requires_admin(client):
    from app.security import hash_password

    db.execute(
        "INSERT INTO users(login, password_hash, role, active, created_at) VALUES('packer2', ?, 'packer', 1, ?)",
        (hash_password("packer123"), db.now_iso()),
    )
    client.post("/login", data={"login": "packer2", "password": "packer123", "next": "/pack"})
    response = client.post("/api/accounts", json={"marketplace": "ozon", "title": "Чужой", "skip_test": True})
    assert response.status_code in (403, 401)


def test_save_cabinet_keys_from_settings(client):
    from app import accounts

    csrf = login(client)
    account_id = accounts.default_account()["id"]
    response = client.post(
        f"/api/accounts/{account_id}",
        json={"title": "Основной", "client_id": "123456", "api_key": "secret-key-value", "skip_test": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    assert accounts.credentials(accounts.get(account_id)) == ("123456", "secret-key-value", "panel")

    page = client.get("/settings")
    assert "123456" in page.text
    assert "secret-key-value" not in page.text, "ключ не должен показываться целиком"


def test_cabinet_keys_are_validated(client):
    from app import accounts

    csrf = login(client)
    account_id = accounts.default_account()["id"]
    response = client.post(
        f"/api/accounts/{account_id}",
        json={"client_id": "кириллица", "api_key": "ключ"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400


def test_add_and_delete_cabinet(client):
    from app import accounts

    csrf = login(client)
    created = client.post(
        "/api/accounts",
        json={"marketplace": "avito", "title": "Avito магазин", "skip_test": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 200, created.text
    new_id = created.json()["account_id"]
    assert accounts.get(new_id)["marketplace"] == "avito"

    deleted = client.post(f"/api/accounts/{new_id}/delete", headers={"X-CSRF-Token": csrf}, json={})
    assert deleted.status_code == 200
    assert accounts.get(new_id) is None


def test_last_cabinet_cannot_be_deleted(client):
    from app import accounts

    csrf = login(client)
    account_id = accounts.default_account()["id"]
    response = client.post(f"/api/accounts/{account_id}/delete", headers={"X-CSRF-Token": csrf}, json={})
    assert response.status_code == 400


def test_switching_cabinet_changes_section(client):
    from app import accounts

    csrf = login(client)
    avito_id = accounts.create("avito", "Avito магазин")
    response = client.post(
        "/api/account/switch", json={"account_id": avito_id, "next": "/orders"}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 200, response.text
    # Раздел FBS в кабинете Avito не открывается — панель ведёт в свой раздел.
    assert response.json()["redirect"] == "/avito"
    assert client.get("/avito").status_code == 200
    assert client.get("/orders").status_code == 409
    assert client.get("/pack").status_code == 409


def test_returns_statuses_endpoint(client):
    csrf = login(client)
    response = client.post(
        "/api/returns/statuses",
        json={"statuses": ["ArrivedAtReturnPlace", "MovingToSeller"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    from app import options

    assert options.get_returns_statuses() == ["ArrivedAtReturnPlace", "MovingToSeller"]
    assert "В пункте выдачи" in response.json()["message"]


def test_returns_statuses_rejects_empty_and_unknown(client):
    csrf = login(client)
    for payload in ({"statuses": []}, {"statuses": ["ЧтоТоНеТо"]}):
        response = client.post("/api/returns/statuses", json=payload, headers={"X-CSRF-Token": csrf})
        assert response.status_code == 400, payload


def test_returns_page_shows_hidden_statuses(client):
    csrf = login(client)
    # Полный обход возвращает все статусы — панель должна сказать, что скрыла лишнее
    client.post("/api/returns/sync", json={"full": True}, headers={"X-CSRF-Token": csrf})
    page = client.get("/returns")
    assert page.status_code == 200
    assert "В пункте выдачи" in page.text
