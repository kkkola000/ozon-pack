"""Акты выдачи возвратов, которые составляет сам Ozon.

Это документ площадки о том же событии, что и наш акт: панель его загружает и
показывает рядом, чтобы было с чем сверить полученное. У Avito такого нет.

Поля метода /v1/return/giveout/* у Ozon менялись, поэтому разбор терпимый, а
сырой ответ сохраняется целиком — проверяем и то, и другое.
"""
import json
import re

import pytest
from fastapi.testclient import TestClient

from app import accounts, db, giveouts, ozon, sync
from app.main import app
from app.ozon import OzonError


@pytest.fixture
def client(sample_data):
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def login(client) -> str:
    response = client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/returns"})
    assert response.status_code == 303, response.text
    page = client.get("/returns")
    return re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1)


def stored():
    return [dict(row) for row in db.query("SELECT * FROM ozon_giveouts ORDER BY id")]


# ---------------------------------------------------------------- загрузка
def test_giveouts_are_loaded_with_returns(sample_data):
    """Акты приезжают вместе с обычной синхронизацией возвратов."""
    rows = stored()
    assert rows, "акты выдачи не загрузились"
    assert all(row["account_id"] == accounts.default_account()["id"] for row in rows)


def test_giveout_keeps_status_and_date(sample_data):
    row = stored()[0]
    assert row["status"], "статус акта не разобран"
    assert row["created_at"], "дата акта не разобрана"
    assert row["status_label"], "статус не переведён на русский"


def test_giveout_keeps_its_items(sample_data):
    row = stored()[0]
    items = json.loads(row["items"])
    assert items, "состав акта не сохранён"
    assert row["items_count"] == len(items)
    assert items[0].get("article_name"), "в составе нет названия товара"


def test_raw_answer_is_kept_whole(sample_data):
    """Сырой ответ нужен целиком: поля метода у Ozon меняются."""
    row = stored()[0]
    raw = json.loads(row["raw"])
    assert raw.get("giveout_id"), "в сыром ответе нет идентификатора"
    assert "articles" in raw, "в сыром ответе нет состава"


def test_second_sync_updates_instead_of_duplicating(sample_data):
    before = stored()
    sync.sync_returns(accounts.default_account())
    after = stored()
    assert len(after) == len(before), "повторная синхронизация размножила акты"
    assert after[0]["first_seen_at"] == before[0]["first_seen_at"], "потеряна дата первой загрузки"


# ---------------------------------------------------------------- устойчивость
def test_missing_method_does_not_break_returns(sample_data, monkeypatch):
    """У части продавцов метод выключен — возвраты всё равно должны обновиться."""
    account = accounts.default_account()
    client = ozon.get_client(account)

    def refuse(*a, **kw):
        raise OzonError("Method not found", status=404)

    monkeypatch.setattr(client, "giveout_list", refuse)
    result = sync.sync_returns(account)
    assert result["returns"] > 0, "возвраты не загрузились из-за актов"
    assert "giveouts_error" in result
    assert "Method not found" in result["giveouts_error"]


def test_act_is_saved_even_without_its_contents(sample_data, monkeypatch):
    """Список дошёл, состав — нет: сам факт выдачи важнее её состава."""
    db.execute("DELETE FROM ozon_giveouts")
    account = accounts.default_account()
    client = ozon.get_client(account)

    def refuse(*a, **kw):
        raise OzonError("giveout info unavailable", status=500)

    monkeypatch.setattr(client, "giveout_info", refuse)
    giveouts.sync_account(account)

    rows = stored()
    assert rows, "акт не сохранился без состава"
    assert rows[0]["items_count"] == 0
    assert json.loads(rows[0]["items"]) == []


def test_unknown_field_names_do_not_crash(sample_data, monkeypatch):
    """Ozon переименовал поля — акт должен сохраниться, а не уронить обход."""
    db.execute("DELETE FROM ozon_giveouts")
    account = accounts.default_account()
    client = ozon.get_client(account)
    monkeypatch.setattr(client, "giveout_list", lambda **kw: ([{"id": "777", "state": "DONE"}], False))
    monkeypatch.setattr(client, "giveout_info", lambda gid: {"products": [{"name": "Что-то"}]})

    result = giveouts.sync_account(account)
    assert result["giveouts"] == 1
    row = stored()[0]
    assert row["id"] == "777"
    assert row["status"] == "DONE"
    assert json.loads(row["items"])[0]["name"] == "Что-то"


def test_act_without_id_is_skipped(sample_data, monkeypatch):
    db.execute("DELETE FROM ozon_giveouts")
    account = accounts.default_account()
    client = ozon.get_client(account)
    monkeypatch.setattr(client, "giveout_list", lambda **kw: ([{"state": "DONE"}], False))
    assert giveouts.sync_account(account)["giveouts"] == 0
    assert stored() == []


def test_status_labels_are_translated():
    assert giveouts.status_label("COMPLETED") == "Выдан"
    assert giveouts.status_label("formed") == "Сформирован"
    # Незнакомый статус показываем как есть, а не прячем
    assert giveouts.status_label("SOMETHING_NEW") == "SOMETHING_NEW"
    assert giveouts.status_label("") == ""


# ---------------------------------------------------------------- на экране
def test_acts_tab_shows_ozon_giveouts(client):
    login(client)
    page = client.get("/returns?tab=acts")
    assert page.status_code == 200
    assert "Акты выдачи Ozon" in page.text
    for row in stored():
        assert str(row["id"]) in page.text, f"акта {row['id']} нет на странице"


def test_main_tab_does_not_show_giveouts(client):
    """Главная страница возвратов осталась прежней."""
    login(client)
    page = client.get("/returns")
    assert "Акты выдачи Ozon" not in page.text


def test_admin_can_see_the_raw_answer(client):
    login(client)
    giveout_id = stored()[0]["id"]
    response = client.get(f"/api/returns/giveouts/{giveout_id}/raw")
    assert response.status_code == 200, response.text
    assert response.json()["raw"]["giveout_id"]


def test_raw_answer_is_admin_only(client):
    from app.security import hash_password

    db.execute(
        "INSERT INTO users(login, password_hash, role, active, created_at) "
        "VALUES('sklad', ?, 'packer', 1, ?)",
        (hash_password("secret123"), db.now_iso()),
    )
    client.post("/login", data={"login": "sklad", "password": "secret123", "next": "/returns"})
    giveout_id = stored()[0]["id"]
    assert client.get(f"/api/returns/giveouts/{giveout_id}/raw").status_code == 403


def test_unknown_giveout_is_404(client):
    login(client)
    assert client.get("/api/returns/giveouts/нет-такого/raw").status_code == 404
