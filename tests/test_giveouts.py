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

from app import accounts, db, giveouts, ozon, return_acts, sync
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


# ---------------------------------------------------------------- видно ли, что акты грузятся
def test_sync_button_reports_the_acts(client):
    """По одной строке «обновлено возвратов» не понять, заработал ли путь по API."""
    csrf = login(client)
    response = client.post("/api/returns/sync", json={}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    message = response.json()["message"]
    assert "Обновлено возвратов" in message
    assert "актов выдачи" in message, message
    assert "возвратов по ним" in message, message


def test_missing_list_falls_back_to_the_document(client, monkeypatch):
    """Списка актов нет — панель берёт у Ozon сам документ выдачи.

    В документе штрихкоды напечатаны, поэтому акт собирается и там, где
    /v1/return/giveout/list выключен.
    """
    from app import ozon

    account = accounts.default_account()
    ozon_client = ozon.get_client(account)
    monkeypatch.setattr(ozon_client, "giveout_list",
                        lambda **kw: (_ for _ in ()).throw(OzonError("Method not found", status=404)))
    db.execute("DELETE FROM return_acts")
    db.execute("UPDATE returns SET act_id = NULL")
    db.execute("DELETE FROM kv WHERE key LIKE 'giveout_doc_try_%'")

    csrf = login(client)
    body = client.post("/api/returns/sync", json={}, headers={"X-CSRF-Token": csrf}).json()
    assert "акты выдачи недоступны" in body["message"]
    assert "акт собран по документу ozon" in body["message"].lower(), body["message"]
    assert return_acts.pending(), "акт по документу не появился"


def test_both_ways_failing_says_what_to_do(client, monkeypatch):
    """Не вышло ни списком, ни документом — предложить загрузку файлом."""
    from app import ozon

    account = accounts.default_account()
    ozon_client = ozon.get_client(account)
    monkeypatch.setattr(ozon_client, "giveout_list",
                        lambda **kw: (_ for _ in ()).throw(OzonError("Method not found", status=404)))
    monkeypatch.setattr(ozon_client, "giveout_pdf",
                        lambda: (_ for _ in ()).throw(OzonError("Giveout disabled", status=403)))
    db.execute("DELETE FROM kv WHERE key LIKE 'giveout_doc_try_%'")

    csrf = login(client)
    body = client.post("/api/returns/sync", json={}, headers={"X-CSRF-Token": csrf}).json()
    assert body["status"] == "warning"
    assert "акты выдачи недоступны" in body["message"]
    assert "загрузить файлом" in body["message"]


def test_sync_button_reports_unmatched_acts(client, monkeypatch):
    """Акт пришёл, а возвраты по нему не опознаны — это не «всё хорошо»."""
    from app import ozon

    account = accounts.default_account()
    ozon_client = ozon.get_client(account)
    monkeypatch.setattr(ozon_client, "giveout_list",
                        lambda **kw: ([{"giveout_id": 9, "giveout_status": "DONE"}], False))
    monkeypatch.setattr(ozon_client, "giveout_info",
                        lambda gid: {"articles": [{"article_name": "Неизвестный товар"}]})
    csrf = login(client)
    message = client.post("/api/returns/sync", json={}, headers={"X-CSRF-Token": csrf}).json()["message"]
    assert "не опознано актов: 1" in message, message


# ---------------------------------------------------------------- документ выдачи
def test_act_is_built_from_the_ozon_document(sample_data):
    """Панель забирает документ у Ozon сама — файл от человека не нужен."""
    account = accounts.default_account()
    db.execute("DELETE FROM return_acts")
    db.execute("UPDATE returns SET act_id = NULL")

    result = giveouts.act_from_document(account)
    assert result["status"] == "ok", result["message"]
    assert result["found"] > 0
    act = return_acts.detail(result["act_id"])
    assert act["total"] == result["found"]
    assert "документ выдачи Ozon" in act["giveout_label"]


def test_document_preview_creates_nothing(sample_data):
    account = accounts.default_account()
    db.execute("DELETE FROM return_acts")
    db.execute("UPDATE returns SET act_id = NULL")

    result = giveouts.act_from_document(account, dry_run=True)
    assert result["status"] == "ok" and result["found"] > 0
    assert return_acts.pending() == [], "предпросмотр завёл акт"


def test_second_document_call_does_not_duplicate(sample_data):
    """Документ у Ozon один: повторный вызов не должен плодить акты."""
    account = accounts.default_account()
    db.execute("DELETE FROM return_acts")
    db.execute("UPDATE returns SET act_id = NULL")

    giveouts.act_from_document(account)
    again = giveouts.act_from_document(account)
    assert again["status"] == "warning"
    assert "уже разнесены" in again["message"]
    assert len(return_acts.pending()) == 1


def test_document_is_not_requested_too_often(sample_data, monkeypatch):
    """Лишний запрос к Ozon каждую минуту не нужен: выдача меняется реже."""
    account = accounts.default_account()
    db.execute("DELETE FROM kv WHERE key LIKE 'giveout_doc_try_%'")
    assert giveouts._document_is_due(account["id"]) is True
    assert giveouts._document_is_due(account["id"]) is False, "документ запрошен второй раз подряд"


def test_document_button_is_admin_only(client):
    from app.security import hash_password

    db.execute(
        "INSERT INTO users(login, password_hash, role, active, created_at) "
        "VALUES('sklad3', ?, 'packer', 1, ?)",
        (hash_password("secret123"), db.now_iso()),
    )
    client.post("/login", data={"login": "sklad3", "password": "secret123", "next": "/returns"})
    page = client.get("/returns")
    csrf = re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1)
    response = client.post("/api/returns/acts/from-ozon", json={}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 403


def test_document_button_works_for_admin(client):
    db.execute("DELETE FROM return_acts")
    db.execute("UPDATE returns SET act_id = NULL")
    csrf = login(client)
    response = client.post("/api/returns/acts/from-ozon", json={}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    assert response.json()["found"] > 0
    assert return_acts.pending(), "акт по документу не появился"


def test_document_failure_is_explained(client, monkeypatch):
    from app import ozon

    ozon_client = ozon.get_client(accounts.default_account())
    monkeypatch.setattr(ozon_client, "giveout_pdf",
                        lambda: (_ for _ in ()).throw(OzonError("Giveout disabled", status=403)))
    csrf = login(client)
    response = client.post("/api/returns/acts/from-ozon", json={}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 502
    assert "Giveout disabled" in response.json()["detail"]


def test_cabinet_without_keys_gets_a_clear_answer(client, monkeypatch):
    """Кабинет без ключей — обычное состояние панели, а не повод для 500.

    Ключи спрашивает get_client, и он тоже кидает OzonError: если не поймать
    его, администратор вместо «внесите ключи» получает пятисотку.
    """
    def no_keys(account=None):
        raise OzonError("У кабинета «Ozon» не заданы ключи Ozon — внесите их в «Настройках»")

    monkeypatch.setattr(giveouts.ozon, "get_client", no_keys)
    csrf = login(client)
    response = client.post("/api/returns/acts/from-ozon", json={}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 502, response.text
    assert "ключи" in response.json()["detail"]
