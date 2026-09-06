"""Проверки HTTP-слоя: доступ, CSRF, основные страницы."""
import re

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture
def client(sample_data):
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


def test_marking_returns_taken_is_gone(client):
    """Отметку «забрали» убрали: статус меняет сама площадка."""
    csrf = login(client)
    return_id = db.query_one("SELECT id FROM returns WHERE is_ready = 1 LIMIT 1")["id"]
    response = client.post("/api/returns/taken", json={"ids": [return_id]}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 404
    assert "Отметить забранными" not in client.get("/returns").text


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


# ---------------------------------------------------------------- адрес перехода
# Проверки startswith("/") было мало: «//evil.com» и «/\evil.com» тоже начинаются
# со слэша, но браузер уходит по ним на чужой домен. Ссылка на настоящий адрес
# панели уводила на её копию сразу после успешного входа.

@pytest.mark.parametrize("target", ["//evil.com", "/\\evil.com", "///evil.com", "https://evil.com", ""])
def test_login_does_not_redirect_outside(client, target):
    response = client.post(
        "/login", data={"login": "admin", "password": "test-admin-pass", "next": target}
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/pack", target


@pytest.mark.parametrize("target", ["/orders", "/orders?tab=deliver", "/returns"])
def test_login_keeps_internal_next(client, target):
    response = client.post(
        "/login", data={"login": "admin", "password": "test-admin-pass", "next": target}
    )
    assert response.headers["location"] == target


def test_switch_account_does_not_redirect_outside(client):
    from app import accounts

    csrf = login(client)
    account_id = accounts.default_account()["id"]
    response = client.post(
        "/api/account/switch",
        json={"account_id": account_id, "next": "//evil.com"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    # Запасной адрес переключения — корень панели, а не чужой домен
    assert response.json()["redirect"] == "/"


def test_login_page_does_not_redirect_outside(client):
    """Уже вошедшему /login отвечает переходом — тоже только внутрь панели."""
    login(client)
    response = client.get("/login?next=//evil.com")
    assert response.status_code == 303
    assert response.headers["location"] == "/pack"


# ---------------------------------------------------------------- сессии
def test_password_change_closes_old_sessions(client):
    """Смена пароля закрывает прежние входы, иначе украденная кука живёт сутки."""
    csrf = login(client)
    user_id = db.query_one("SELECT id FROM users WHERE login = 'admin'")["id"]
    assert client.get("/pack").status_code == 200

    response = client.post(
        f"/api/users/{user_id}",
        json={"password": "новый-пароль-1"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text

    # Кука осталась прежней, но панель её больше не принимает
    assert client.get("/pack").status_code == 303
    assert client.get("/api/state").status_code == 401


def test_password_change_leaves_other_users_alone(client):
    from app.security import hash_password

    csrf = login(client)
    db.execute(
        "INSERT INTO users(login, password_hash, role, active, created_at) VALUES('packer3', ?, 'packer', 1, ?)",
        (hash_password("packer123"), db.now_iso()),
    )
    other_id = db.query_one("SELECT id FROM users WHERE login = 'packer3'")["id"]
    response = client.post(
        f"/api/users/{other_id}", json={"password": "другой-пароль"}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 200, response.text
    # Свою сессию смена чужого пароля не рвёт
    assert client.get("/pack").status_code == 200


# ---------------------------------------------------------------- прочее
def test_public_paths_match_exactly(client):
    """Раньше startswith пускал бы без входа любой адрес, начинающийся с /login."""
    for path in ("/login-sso", "/healthz-details", "/staticfiles"):
        response = client.get(path)
        assert response.status_code == 303, f"{path} открылся без входа"
        assert response.headers["location"].startswith("/login?next=")
    # Настоящие публичные адреса продолжают работать
    assert client.get("/login").status_code == 200
    assert client.get("/healthz").status_code == 200


def test_csrf_rejects_token_with_right_prefix(client):
    csrf = login(client)
    response = client.post("/api/scan", json={"code": "1"}, headers={"X-CSRF-Token": csrf[:-1] + "x"})
    assert response.status_code == 403


def test_database_file_is_not_world_readable(client):
    """В базе ключи площадок открытым текстом и хеши паролей."""
    import stat

    from pathlib import Path

    from app.config import settings

    mode = stat.S_IMODE(Path(settings.db_path).stat().st_mode)
    assert mode == 0o600, oct(mode)


# ---------------------------------------------------------------- ответы площадки
# Имя файла и адрес готового стикера приходят из ответа Ozon. В заголовок ответа
# и в исходящий запрос они попадают как есть, поэтому чистим и проверяем.

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("label.pdf", "label.pdf"),
        ('x"; attachment; filename="evil.exe', "x_attachment_filename_evil.exe"),
        ("../../etc/passwd", ".._.._etc_passwd"),
        ("стикер.pdf", "_.pdf"),
        ("отчёт\r\nSet-Cookie: a=b", "_Set-Cookie_a_b"),
        ("", "label.pdf"),
        (None, "label.pdf"),
        ("...", "label.pdf"),
    ],
)
def test_safe_filename(raw, expected):
    from app.deps import safe_filename

    assert safe_filename(raw) == expected


def test_label_header_survives_hostile_filename(client, monkeypatch):
    from app import accounts, ozon

    login(client)
    fake = ozon.get_client(accounts.default_account())
    # Стикер отдаёт сама подделка, подменяем только имя файла в её ответе
    original = fake.package_label
    monkeypatch.setattr(
        fake, "package_label",
        lambda numbers: (original(numbers)[0], 'a"; filename="evil.exe'),
    )

    number = db.query_one(
        "SELECT posting_number FROM postings WHERE status = 'awaiting_deliver' LIMIT 1"
    )["posting_number"]
    response = client.get(f"/api/label/{number}.pdf")
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert disposition == 'inline; filename="a_filename_evil.exe"', disposition
    # Заголовок не разорван: второго filename в нём нет
    assert disposition.count("filename=") == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.com/label.pdf",
        "//evil.com/label.pdf",
        "http://api-seller.ozon.ru/label.pdf",
        "http://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
    ],
)
def test_label_url_from_response_must_stay_on_ozon(url):
    """По адресу из ответа панель ходит сама — увести её в чужую сеть нельзя."""
    from app.ozon import OzonClient, OzonError

    client_obj = OzonClient(client_id="x", api_key="y")
    try:
        with pytest.raises(OzonError):
            client_obj._same_host_url(url)
    finally:
        client_obj.close()


@pytest.mark.parametrize("url", ["https://api-seller.ozon.ru/f/1.pdf", "/f/1.pdf", "f/1.pdf"])
def test_label_url_on_same_host_is_allowed(url):
    from app.ozon import OzonClient

    client_obj = OzonClient(client_id="x", api_key="y")
    try:
        assert client_obj._same_host_url(url) == url
    finally:
        client_obj.close()


def test_login_form_does_not_carry_hostile_next(client):
    """Скрытое поле формы не должно носить чужой домен."""
    page = client.get("/login?next=//evil.com").text
    assert 'name="next" value="/pack"' in page
    assert "evil.com" not in page
