"""«Принтеры»: для каждого документа — размер листа и принтер QZ Tray.

Печать через браузер остаётся как была; QZ Tray — второй путь. Настраивается
по документам: стикеры Ozon, ярлыки Маркета, этикетки Avito, лист и акт
возвратов. У документа бывает несколько размеров (этикетка Avito — 58×40 или
100×150): тогда принтер выбирается по настоящему размеру PDF.

Страница доступна всем, кто работает в панели, включая сборщика, — и только
она из настроек. Чтобы QZ Tray не спрашивал разрешение на каждое подключение,
панель подписывает его запросы своим ключом: ключ не покидает сервер.
"""
import base64
import hashlib
import io
import json
import os
import re
import stat
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi.testclient import TestClient

from app.core import db, printers
from app.core.config import BASE_DIR, settings
from app.core.security import hash_password
from app.main import app
from app.markets.yandex import client as yandex_client


def enter(client, login="admin", password="test-admin-pass") -> str:
    response = client.post("/login", data={"login": login, "password": password, "next": "/pack"})
    assert response.status_code == 303, response.text
    # Токен — со «Сборки»: её открывает любой, кто печатает.
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


def save(client, csrf, rows):
    return client.post("/api/printers", json={"rows": rows}, headers={"X-CSRF-Token": csrf})


def row(kind, size, printer=""):
    return {"kind": kind, "size": size, "printer": printer}


# ------------------------------------------------------------------ документы
def test_every_document_has_its_own_row():
    """Наклейки каждой площадки — отдельно, плюс лист и акт возвратов."""
    kinds = [doc["kind"] for doc in printers.documents()]
    assert {"ozon:label", "avito:label", "yandex:label", "returns:sheet", "returns:act"} <= set(kinds)
    titles = {doc["kind"]: doc["title"] for doc in printers.documents()}
    assert titles["ozon:label"] == "Ozon: стикеры"
    assert titles["avito:label"] == "Avito: этикетки"


def test_by_default_nothing_changes():
    """Пока никто не выбрал — всё через браузер, на тех же листах, что и раньше."""
    table = {r["kind"]: r for r in printers.rows()}
    assert all(r["printer"] == "" for r in table.values())
    assert table["ozon:label"]["size"] == "75x120"
    assert table["returns:act"]["size"] == "a4"


def test_the_warehouse_setup_is_saved(client):
    """Как на складе: акт — A4, стикеры Ozon и ярлыки Маркета — 58×40, Avito — два
    размера на двух принтерах. И все принтеры разные."""
    csrf = enter(client)
    rows = [
        row("ozon:label", "58x40", "Xprinter Ozon"),
        row("yandex:label", "58x40", "Xprinter Маркет"),
        row("avito:label", "58x40", "Xprinter Avito"),
        row("avito:label", "100x150", "Zebra Avito"),
        row("returns:sheet", "a4", "HP LaserJet"),
        row("returns:act", "a4", "HP LaserJet"),
    ]
    response = save(client, csrf, rows)
    assert response.status_code == 200, response.text
    table = printers.rows()
    assert [r for r in table if r["kind"] == "avito:label"] == [
        row("avito:label", "58x40", "Xprinter Avito"), row("avito:label", "100x150", "Zebra Avito"),
    ]
    assert printers.size_of("yandex:label") == "58x40"
    event = db.query_one("SELECT login, message FROM events WHERE kind = 'printers_saved'")
    assert event["login"] == "admin" and "Zebra Avito" in event["message"]
    # В журнале — названия документов, как на странице, а не внутренние коды
    assert "Акт возврата, A4: HP LaserJet" in event["message"]
    assert "avito:label" not in event["message"]


def test_a_forgotten_document_keeps_its_default(client):
    csrf = enter(client)
    save(client, csrf, [row("ozon:label", "58x40", "Xprinter")])
    table = {r["kind"]: r for r in printers.rows()}
    assert table["ozon:label"]["printer"] == "Xprinter"
    assert table["returns:act"] == row("returns:act", "a4", "")


@pytest.mark.parametrize("rows, why", [
    ([row("poster", "a4")], "неизвестный документ"),
    ([row("ozon:label", "10x10")], "неизвестный размер"),
    ([row("ozon:label", "58x40"), row("ozon:label", "58x40")], "один размер дважды"),
    ([row("avito:label", s) for s in ("58x40", "75x120", "100x150", "a4")] + [row("avito:label", "58x40")],
     "слишком много размеров"),
    ([row("ozon:label", "58x40", "x" * 500)], "слишком длинное имя"),
])
def test_nonsense_is_refused(client, rows, why):
    csrf = enter(client)
    assert save(client, csrf, rows).status_code == 400, why
    assert all(r["printer"] == "" for r in printers.rows())


def test_names_are_tidied(client):
    csrf = enter(client)
    save(client, csrf, [row("returns:act", "a4", "  HP   LaserJet  ")])
    assert {r["kind"]: r for r in printers.rows()}["returns:act"]["printer"] == "HP LaserJet"


def test_the_old_setting_is_carried_over():
    """В 1.42.0 принтер выбирался на размер «наклейка» и «A4» — выбор не пропадает."""
    db.kv_set("printer:label", "Zebra")
    db.kv_set("printer:a4", "HP")
    table = {r["kind"]: r for r in printers.rows()}
    assert table["ozon:label"]["printer"] == "Zebra" and table["avito:label"]["printer"] == "Zebra"
    assert table["returns:sheet"]["printer"] == "HP" and table["returns:act"]["printer"] == "HP"


# ------------------------------------------------------------------ доступ
@pytest.mark.parametrize("role", ["packer", "admin"])
def test_everyone_sets_up_printers(client, role):
    """Страница и сохранение — всем, включая сборщика: он у стола и знает принтеры."""
    person("someone", role)
    csrf = enter(client, "someone", "secret-password-1")
    page = client.get("/printers")
    assert page.status_code == 200
    assert "Avito: этикетки" in page.text
    assert save(client, csrf, [row("ozon:label", "58x40", "Xprinter")]).status_code == 200
    assert db.query_one("SELECT login FROM events WHERE kind = 'printers_saved'")["login"] == "someone"


def test_a_packer_gets_only_the_printers(client):
    """Сборщику — только «Принтеры»: остальные настройки ему по-прежнему закрыты."""
    person("petrov", "packer")
    enter(client, "petrov", "secret-password-1")
    assert client.get("/settings").status_code == 403
    assert 'href="/printers"' in client.get("/pack").text


def test_settings_point_to_the_printers(client):
    enter(client)
    assert 'href="/printers"' in client.get("/settings").text


def test_strangers_are_turned_away(client):
    assert client.post("/api/printers", json={"rows": []}).status_code == 401
    assert client.get("/printers").status_code in (303, 401)


def test_saving_needs_csrf(client):
    enter(client)
    assert client.post("/api/printers", json={"rows": []}).status_code == 403


def test_every_page_knows_the_setup(client):
    """Печатают «Сборка», «Заказы» и «Возвраты» — настройка приходит в каждую страницу."""
    csrf = enter(client)
    save(client, csrf, [row("avito:label", "58x40", "A"), row("avito:label", "100x150", "B")])
    for where in ("/pack", "/orders", "/returns", "/printers"):
        page = client.get(where).text
        found = re.search(r"window\.PRINTERS = (\{.*?\});</script>", page)
        assert found, where
        setup = json.loads(found.group(1))
        assert setup["paper"]["100x150"] == [100, 150]
        assert row("avito:label", "100x150", "B") in setup["rows"]
        assert "/static/qz.js" in page, where


def test_print_links_name_their_document(client):
    """«Печать листа» и «Печать акта» знают свой документ и свой PDF."""
    enter(client)
    page = client.get("/returns").text
    assert 'data-print-pdf="/returns/sheet.pdf' in page
    assert 'data-print-kind="returns:sheet"' in page


def test_the_library_is_pinned():
    """qz-tray.js лежит у нас и нужной версии: панель не тянет чужой скрипт из сети."""
    library = BASE_DIR / "app" / "static" / "vendor" / "qz-tray.js"
    assert "@version 2.3.0" in library.read_text(encoding="utf-8")[:300]


# ------------------------------------------------------------------ размер листа
def pdf_of(width_mm: float, height_mm: float, rotate: int = 0) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    page = writer.add_blank_page(width=width_mm * 72 / 25.4, height=height_mm * 72 / 25.4)
    if rotate:
        page.rotate(rotate)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_the_page_size_is_measured():
    assert printers.page_size(pdf_of(58, 40)) == "58x40"
    assert printers.page_size(pdf_of(100, 150)) == "100x150"
    assert printers.page_size(pdf_of(210, 297)) == "210x297"
    # Страница, повёрнутая на 90°, печатается как повёрнутая.
    assert printers.page_size(pdf_of(40, 58, rotate=90)) == "58x40"
    assert printers.page_size(b"not a pdf") is None


def test_label_answers_carry_the_page_size(client):
    """Ответ с наклейкой несёт размер листа — по нему браузер выбирает принтер."""
    enter(client)
    number = db.query_one("SELECT posting_number FROM postings WHERE status = 'awaiting_deliver' LIMIT 1")[0]
    response = client.get(f"/api/pack/label/ozon/{number}.pdf")
    assert response.status_code == 200, response.text
    assert response.headers["X-Page-Size"] == "75x120"   # тестовый стикер 75×120


# ------------------------------------------------------------------ формат Маркета
def test_market_labels_follow_the_chosen_size(client, yandex_account):
    """Лента 58×40 у ярлыков Маркета — панель и просит у Маркета ярлык 58×40."""
    assert yandex_client.label_format() == "A7"          # как было всегда
    csrf = enter(client)
    save(client, csrf, [row("yandex:label", "58x40", "Xprinter")])
    assert yandex_client.label_format() == "A9_HORIZONTALLY"
    save(client, csrf, [row("yandex:label", "100x150", "Zebra")])
    assert yandex_client.label_format() == "A7"          # 100×150 Маркет не делает


def test_the_format_goes_into_the_request(client, yandex_account):
    """Не только функция: настоящий запрос ярлыков уходит с этим форматом."""
    csrf = enter(client)
    save(client, csrf, [row("yandex:label", "58x40")])
    fake = yandex_client.get_client(yandex_account)
    sent = {}

    def request(method, path, **kwargs):
        sent.update(kwargs.get("params") or {}, path=path)
        return httpx.Response(200, content=b"%PDF-1.4 label")

    fake._request = request
    yandex_client.YandexClient.order_labels(fake, 21000000, 80000001)
    assert sent["format"] == "A9_HORIZONTALLY"
    assert sent["path"] == "/v2/campaigns/21000000/orders/80000001/delivery/labels"


# ------------------------------------------------------------------ сертификат
def test_the_certificate_is_made_once(client):
    enter(client)
    first = client.get("/api/printers/qz/certificate")
    assert first.status_code == 200
    assert first.text.startswith("-----BEGIN CERTIFICATE-----")
    assert client.get("/api/printers/qz/certificate").text == first.text
    x509.load_pem_x509_certificate(first.text.encode())


def test_the_certificate_downloads_as_override_crt(client):
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
    """Печатает сборщик — ему подпись и нужна."""
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
