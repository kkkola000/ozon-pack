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
        """SELECT pb.barcode FROM product_barcodes pb JOIN posting_items i ON i.sku = pb.sku
           JOIN postings p ON p.posting_number = i.posting_number
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
