"""«Настройка принтеров»: принтер QZ Tray на каждый размер листа.

Печать через браузер остаётся как была; QZ Tray — второй путь. Выбирает его
владелец, пустое значение значит «через браузер». Чтобы QZ Tray не спрашивал
разрешение на каждое подключение, панель подписывает его запросы своим ключом:
ключ не покидает сервер, наружу уходит только сертификат.
"""
import base64
import hashlib
import json
import os
import re
import stat
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi.testclient import TestClient

from app.core import db, printers
from app.core.config import BASE_DIR, settings
from app.core.security import hash_password
from app.main import app


def enter(client, login="admin", password="test-admin-pass") -> str:
    response = client.post("/login", data={"login": login, "password": password, "next": "/pack"})
    assert response.status_code == 303, response.text
    # Токен — со «Сборки»: её открывает любой, кто печатает, а «Настройки» — не каждый.
    page = client.get("/pack")
    return re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1)


def person(login: str, role: str) -> None:
    db.execute(
        "INSERT INTO users(login, password_hash, role, active, created_at) VALUES(?, ?, ?, 1, ?)",
        (login, hash_password("secret-password-1"), role, db.now_iso()),
    )


@pytest.fixture
def client(sample_data):
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def save(client, csrf, chosen):
    return client.post("/api/printers", json={"printers": chosen}, headers={"X-CSRF-Token": csrf})


# ------------------------------------------------------------------ выбор принтеров
def test_by_default_everything_prints_through_the_browser():
    """Ничего не выбрано — печать как была: через браузер."""
    assert printers.printers() == {"label": "", "a4": ""}


def test_owner_picks_a_printer_per_size(client):
    csrf = enter(client)
    response = save(client, csrf, {"label": "Xprinter XP-420B", "a4": ""})
    assert response.status_code == 200, response.text
    assert printers.printers() == {"label": "Xprinter XP-420B", "a4": ""}
    assert db.query_one("SELECT 1 FROM events WHERE kind = 'printers_saved'")


def test_empty_brings_the_browser_back(client):
    """Выбрали «через браузер» — принтер снимается, печать снова как раньше."""
    csrf = enter(client)
    save(client, csrf, {"label": "Xprinter XP-420B"})
    save(client, csrf, {"label": ""})
    assert printers.printers()["label"] == ""


def test_names_are_tidied(client):
    csrf = enter(client)
    save(client, csrf, {"a4": "  HP   LaserJet  "})
    assert printers.printers()["a4"] == "HP LaserJet"


def test_nonsense_is_refused(client):
    csrf = enter(client)
    assert save(client, csrf, {"poster": "HP"}).status_code == 400
    assert save(client, csrf, {"label": "x" * 500}).status_code == 400
    assert printers.printers() == {"label": "", "a4": ""}


@pytest.mark.parametrize("role", ["admin", "packer"])
def test_only_the_owner_chooses_printers(client, role):
    """Настройка владельца: администратор и сборщик её не меняют."""
    person("someone", role)
    csrf = enter(client, "someone", "secret-password-1")
    assert save(client, csrf, {"label": "HP"}).status_code == 403
    assert printers.printers()["label"] == ""


def test_saving_needs_csrf(client):
    enter(client)
    assert client.post("/api/printers", json={"printers": {"label": "HP"}}).status_code == 403


def test_every_page_knows_the_choice(client):
    """Печатают «Сборка», «Заказы» и «Возвраты» — выбор приходит в каждую страницу."""
    csrf = enter(client)
    save(client, csrf, {"label": "Xprinter XP-420B", "a4": "HP LaserJet"})
    for where in ("/pack", "/orders", "/returns"):
        page = client.get(where).text
        found = re.search(r"window\.PRINTERS = (\{.*?\});", page)
        assert found, where
        assert json.loads(found.group(1)) == {"label": "Xprinter XP-420B", "a4": "HP LaserJet"}
        assert "/static/qz.js" in page, where


def test_the_panel_is_only_for_the_owner(client):
    enter(client)
    assert "Настройка принтеров" in client.get("/settings").text

    person("manager", "admin")
    with TestClient(app, follow_redirects=False) as other:
        enter(other, "manager", "secret-password-1")
        assert "Настройка принтеров" not in other.get("/settings").text


def test_a4_links_carry_their_pdf(client):
    """«Печать листа» знает свой PDF: с принтером A4 он уходит на него, а не в окно браузера."""
    enter(client)
    page = client.get("/returns").text
    assert 'data-print-pdf="/returns/sheet.pdf' in page
    assert 'data-print-size="a4"' in page


def test_the_library_is_pinned():
    """qz-tray.js лежит у нас и нужной версии: панель не тянет чужой скрипт из сети."""
    library = BASE_DIR / "app" / "static" / "vendor" / "qz-tray.js"
    assert "@version 2.3.0" in library.read_text(encoding="utf-8")[:300]


# ------------------------------------------------------------------ сертификат
def test_the_certificate_is_made_once(client):
    enter(client)
    first = client.get("/api/printers/qz/certificate")
    assert first.status_code == 200
    assert first.text.startswith("-----BEGIN CERTIFICATE-----")
    assert client.get("/api/printers/qz/certificate").text == first.text
    x509.load_pem_x509_certificate(first.text.encode())


def test_the_certificate_downloads_as_a_file(client):
    enter(client)
    response = client.get("/api/printers/qz/certificate?download=1")
    assert "attachment" in response.headers["content-disposition"]
    assert "override.crt" in response.headers["content-disposition"]


def test_the_private_key_stays_private(client):
    """Ключ — только на сервере, с правами 0600, и наружу не уходит никак."""
    enter(client)
    text = client.get("/api/printers/qz/certificate").text
    assert "PRIVATE KEY" not in text
    key = Path(settings.db_path).parent / "qz" / "private-key.pem"
    assert key.exists()
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600
    assert "static" not in key.parts


def test_the_certificate_needs_a_login(client):
    assert client.get("/api/printers/qz/certificate").status_code == 401


# ------------------------------------------------------------------ подпись
def test_the_signature_matches_the_certificate(client):
    """Подпись сверяется сертификатом панели — ровно так её проверяет QZ Tray."""
    csrf = enter(client)
    to_sign = hashlib.sha256(b'{"call":"print"}').hexdigest()
    response = client.post("/api/printers/qz/sign", content=to_sign,
                           headers={"X-CSRF-Token": csrf, "Content-Type": "text/plain"})
    assert response.status_code == 200, response.text
    cert = x509.load_pem_x509_certificate(client.get("/api/printers/qz/certificate").text.encode())
    cert.public_key().verify(base64.b64decode(response.text), to_sign.encode(),
                             padding.PKCS1v15(), hashes.SHA512())


def test_a_packer_can_sign(client):
    """Печатает сборщик — ему подпись и нужна, настройка принтеров тут ни при чём."""
    person("petrov", "packer")
    csrf = enter(client, "petrov", "secret-password-1")
    to_sign = hashlib.sha256(b"x").hexdigest()
    response = client.post("/api/printers/qz/sign", content=to_sign,
                           headers={"X-CSRF-Token": csrf, "Content-Type": "text/plain"})
    assert response.status_code == 200


def test_only_qz_hashes_are_signed(client):
    """Подписываем только то, что присылает QZ Tray, — ключ не подпись для всего подряд."""
    csrf = enter(client)
    for body in ("hello", "A" * 64, hashlib.sha256(b"x").hexdigest() + "0"):
        response = client.post("/api/printers/qz/sign", content=body,
                               headers={"X-CSRF-Token": csrf, "Content-Type": "text/plain"})
        assert response.status_code == 400, body


def test_signing_needs_a_login_and_csrf(client):
    to_sign = hashlib.sha256(b"x").hexdigest()
    assert client.post("/api/printers/qz/sign", content=to_sign).status_code == 401
    enter(client)
    assert client.post("/api/printers/qz/sign", content=to_sign).status_code == 403
