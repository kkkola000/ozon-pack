"""Роли и доступ к разделам.

Роль отвечает за то, что человеку можно делать с панелью, разделы — какие
экраны он видит. Раньше это была одна вещь: администратор видел всё, сборщик
три экрана, середины не существовало.

Главное, что здесь проверяется: администратор не может дотянуться до владельца
— ни до роли, ни до разделов, ни до пароля. Иначе «администратор» и «владелец»
были бы одним и тем же: первый вторым же ключом и открыл бы себе всё.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app.core import access, db, security
from app.main import app


def make_user(login, role, sections=None, password="parol1234567"):
    db.execute(
        "INSERT INTO users(login, password_hash, role, sections, active, created_at) VALUES(?,?,?,?,1,?)",
        (login, security.hash_password(password), role,
         None if sections is None else access.dump_sections(role, sections), db.now_iso()),
    )
    return dict(db.query_one("SELECT * FROM users WHERE login = ?", (login,)))


@pytest.fixture
def client(sample_data):
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def sign_in(client, login, password) -> str:
    response = client.post("/login", data={"login": login, "password": password, "next": "/pack"})
    assert response.status_code == 303, response.text
    page = client.get("/pack")
    found = re.search(r'name="csrf-token" content="([^"]*)"', page.text)
    return found.group(1) if found else ""


def as_owner(client) -> str:
    return sign_in(client, "admin", "test-admin-pass")


# ------------------------------------------------------------------ роли
def test_installer_is_the_owner(sample_data):
    """Первая учётка — владелец: иначе некому назначать владельцев."""
    row = db.query_one("SELECT role FROM users WHERE login = 'admin'")
    assert row["role"] == access.OWNER


def test_owner_sees_every_section(sample_data):
    owner = dict(db.query_one("SELECT * FROM users WHERE login = 'admin'"))
    assert access.sections_of(owner) == access.SECTION_KEYS


def test_owner_sections_cannot_be_cut(sample_data):
    """Урезанный владелец — это панель, которую некому чинить."""
    owner = make_user("vlad", access.OWNER, sections=["pack"])
    assert access.sections_of(owner) == access.SECTION_KEYS


def test_packer_gets_the_warehouse_sections_by_default(sample_data):
    packer = make_user("sborshik", access.PACKER)
    assert access.sections_of(packer) == ["pack", "orders", "returns"]


def test_settings_are_never_given_to_a_packer(sample_data):
    """Там ключи площадок и выдача прав — это работа администратора."""
    packer = make_user("sborshik2", access.PACKER, sections=["pack", "settings"])
    assert "settings" not in access.sections_of(packer)
    assert access.clean_sections(access.PACKER, ["settings"]) == []


def test_empty_list_means_nothing_is_shown(sample_data):
    """Пустой список — осознанный выбор, а не «настройки не трогали»."""
    quiet = make_user("nikto", access.PACKER, sections=[])
    assert access.sections_of(quiet) == []


def test_admin_cannot_touch_the_owner(sample_data):
    admin = make_user("adm", access.ADMIN)
    owner = dict(db.query_one("SELECT * FROM users WHERE login = 'admin'"))
    packer = make_user("pak", access.PACKER)

    assert access.may_manage(admin, packer) is True
    assert access.may_manage(admin, owner) is False
    assert access.may_manage(owner, owner) is True
    assert access.may_manage(packer, packer) is False


def test_only_the_owner_appoints_an_owner(sample_data):
    admin = make_user("adm2", access.ADMIN)
    owner = dict(db.query_one("SELECT * FROM users WHERE login = 'admin'"))
    assert access.may_set_role(admin, access.ADMIN) is True
    assert access.may_set_role(admin, access.OWNER) is False
    assert access.may_set_role(owner, access.OWNER) is True


# ------------------------------------------------------------------ экраны
SECTION_URLS = {
    "orders": "/orders",
    "returns": "/returns",
    "products": "/products",
    "reports": "/reports",
    "logs": "/logs",
    "settings": "/settings",
}


def test_granted_section_opens_for_a_packer(client):
    """Сборщик с выданными «Отчётами» их открывает — это и есть смысл галочек."""
    make_user("reporter", access.PACKER, sections=["pack", "reports"])
    sign_in(client, "reporter", "parol1234567")
    assert client.get("/reports").status_code == 200


def test_sections_that_were_not_given_stay_closed(client):
    make_user("tolko-pack", access.PACKER, sections=["pack"])
    sign_in(client, "tolko-pack", "parol1234567")
    for section, url in SECTION_URLS.items():
        assert client.get(url).status_code == 403, f"{section} открылся без выдачи"


def test_admin_without_a_section_is_refused_too(client):
    """Разделы отдельно от роли: администратор без раздела его не видит."""
    make_user("adm3", access.ADMIN, sections=["settings"])
    sign_in(client, "adm3", "parol1234567")
    assert client.get("/reports").status_code == 403
    assert client.get("/settings").status_code == 200


def test_refusal_names_the_section(client):
    make_user("adm4", access.ADMIN, sections=["settings"])
    sign_in(client, "adm4", "parol1234567")
    detail = client.get("/reports", headers={"Accept": "application/json"}).json()["detail"]
    assert "Отчёты" in detail


def test_menu_shows_only_granted_sections(client):
    make_user("reporter2", access.PACKER, sections=["pack", "reports"])
    sign_in(client, "reporter2", "parol1234567")
    page = client.get("/pack").text
    assert 'href="/reports"' in page
    assert 'href="/logs"' not in page
    assert 'href="/settings"' not in page


def test_taking_a_section_away_closes_it_at_once(client):
    """Снятая галочка должна закрывать экран сразу, а не после нового входа."""
    user = make_user("tekuschiy", access.PACKER, sections=["pack", "reports"])
    sign_in(client, "tekuschiy", "parol1234567")
    assert client.get("/reports").status_code == 200

    db.execute("UPDATE users SET sections = ? WHERE id = ?",
               (access.dump_sections(access.PACKER, ["pack"]), user["id"]))
    assert client.get("/reports").status_code == 403


# ------------------------------------------------------------------ выдача прав
def test_owner_changes_anyones_password(client):
    csrf = as_owner(client)
    other = make_user("vlad2", access.OWNER)
    response = client.post(f"/api/users/{other['id']}", json={"password": "novyjparol12345"},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    row = db.query_one("SELECT password_hash FROM users WHERE id = ?", (other["id"],))
    assert security.verify_password("novyjparol12345", row["password_hash"])


def test_admin_cannot_change_the_owners_password(client):
    """Иначе администратор просто войдёт владельцем."""
    admin = make_user("adm5", access.ADMIN)
    owner = db.query_one("SELECT id FROM users WHERE login = 'admin'")
    csrf = sign_in(client, "adm5", "parol1234567")
    response = client.post(f"/api/users/{owner['id']}", json={"password": "chuzhoyparol123"},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 403
    assert "владельца" in response.json()["detail"].lower()
    assert admin


def test_admin_cannot_change_the_owners_role_or_sections(client):
    owner = db.query_one("SELECT id FROM users WHERE login = 'admin'")
    make_user("adm6", access.ADMIN)
    csrf = sign_in(client, "adm6", "parol1234567")
    for payload in ({"role": "packer"}, {"sections": ["pack"]}, {"active": False}):
        response = client.post(f"/api/users/{owner['id']}", json=payload,
                               headers={"X-CSRF-Token": csrf})
        assert response.status_code == 403, payload
    assert db.query_one("SELECT role, active FROM users WHERE id = ?", (owner["id"],))["role"] == access.OWNER


def test_admin_cannot_promote_anyone_to_owner(client):
    packer = make_user("pak2", access.PACKER)
    make_user("adm7", access.ADMIN)
    csrf = sign_in(client, "adm7", "parol1234567")
    response = client.post(f"/api/users/{packer['id']}", json={"role": "owner"},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 403
    assert db.query_one("SELECT role FROM users WHERE id = ?", (packer["id"],))["role"] == access.PACKER


def test_admin_manages_a_packer(client):
    """Ради этого администратор и нужен: доступы выдаёт он, а не только владелец."""
    packer = make_user("pak3", access.PACKER)
    make_user("adm8", access.ADMIN)
    csrf = sign_in(client, "adm8", "parol1234567")
    response = client.post(f"/api/users/{packer['id']}",
                           json={"sections": ["pack", "reports"]}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    row = dict(db.query_one("SELECT * FROM users WHERE id = ?", (packer["id"],)))
    assert access.sections_of(row) == ["pack", "reports"]


def test_a_packer_cannot_grant_anything(client):
    packer = make_user("pak4", access.PACKER)
    sign_in(client, "pak4", "parol1234567")
    assert client.post(f"/api/users/{packer['id']}", json={"sections": []}).status_code == 403
    assert client.post("/api/users", json={"login": "novyj", "password": "parol1234567"}).status_code == 403


def test_owner_sections_are_not_editable_through_the_api(client):
    csrf = as_owner(client)
    other = make_user("vlad3", access.OWNER)
    response = client.post(f"/api/users/{other['id']}", json={"sections": ["pack"]},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 400
    assert "все разделы" in response.json()["detail"]


def test_the_last_owner_stays(client):
    """Без владельца панель чинить пришлось бы руками в базе."""
    csrf = as_owner(client)
    owner = db.query_one("SELECT id FROM users WHERE login = 'admin'")
    for payload in ({"role": "admin"}, {"active": False}):
        response = client.post(f"/api/users/{owner['id']}", json=payload,
                               headers={"X-CSRF-Token": csrf})
        assert response.status_code == 400, payload
        assert "владел" in response.json()["detail"].lower()


def test_owner_can_step_down_when_there_is_another(client):
    csrf = as_owner(client)
    second = make_user("vlad4", access.OWNER)
    owner = db.query_one("SELECT id FROM users WHERE login = 'admin'")
    response = client.post(f"/api/users/{owner['id']}", json={"role": "admin"},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    assert db.query_one("SELECT role FROM users WHERE id = ?", (second["id"],))["role"] == access.OWNER


def test_role_change_drops_sections_the_new_role_may_not_have(client):
    """Был администратором с «Настройками», стал сборщиком — доступ уходит."""
    csrf = as_owner(client)
    user = make_user("byvshiy", access.ADMIN, sections=["pack", "settings"])
    response = client.post(f"/api/users/{user['id']}",
                           json={"role": "packer", "sections": ["pack", "settings"]},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    row = dict(db.query_one("SELECT * FROM users WHERE id = ?", (user["id"],)))
    assert access.sections_of(row) == ["pack"]


def test_new_user_gets_the_role_defaults(client):
    csrf = as_owner(client)
    response = client.post("/api/users",
                           json={"login": "novichok", "password": "parol1234567", "role": "packer"},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    row = dict(db.query_one("SELECT * FROM users WHERE login = 'novichok'"))
    assert access.sections_of(row) == ["pack", "orders", "returns"]


def test_settings_page_shows_the_grid(client):
    as_owner(client)
    make_user("pak5", access.PACKER)
    page = client.get("/settings").text
    assert "Сотрудники и доступы" in page
    for _key, label, _url, _hint in access.SECTIONS:
        assert label in page
    assert "data-section-for" in page and "data-role-for" in page


def test_admin_sees_the_owner_but_no_buttons(client):
    make_user("adm9", access.ADMIN)
    sign_in(client, "adm9", "parol1234567")
    page = client.get("/settings").text
    owner = db.query_one("SELECT id FROM users WHERE login = 'admin'")
    assert "admin" in page
    assert f'data-save-user="{owner["id"]}"' not in page, "администратору предложили менять владельца"
