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
    assert "актов выдачи у Ozon" in message, message
    assert "Ждёт подтверждения" in message, message


def test_sync_does_not_create_acts_by_itself(client):
    """Синхронизация приносит список, но поездки заводит человек.

    Иначе выбирать будет нечего: к моменту, когда администратор откроет
    список, все акты уже окажутся заведёнными.
    """
    db.execute("DELETE FROM return_acts")
    db.execute("UPDATE returns SET act_id = NULL")
    sync.sync_returns(accounts.default_account())

    from_ozon = [act for act in return_acts.pending() if act["from_ozon"]]
    assert from_ozon == [], "синхронизация завела акты сама"
    assert stored(), "акты Ozon не загрузились — выбирать будет не из чего"


def test_missing_list_says_what_to_do(client, monkeypatch):
    """Списка актов нет — назвать причину и запасные ходы."""
    from app import ozon

    ozon_client = ozon.get_client(accounts.default_account())
    monkeypatch.setattr(ozon_client, "giveout_list",
                        lambda **kw: (_ for _ in ()).throw(OzonError("Method not found", status=404)))

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
# ---------------------------------------------------------------- список актов на выбор
def test_available_asks_ozon_for_the_act_list(client):
    """Кнопка «Получить акты возвратов» спрашивает список у Ozon и показывает его.

    Сама ничего не заводит: какие поездки добавить в панель — решает человек.
    """
    db.execute("DELETE FROM return_acts")
    db.execute("UPDATE returns SET act_id = NULL")
    login(client)

    response = client.get("/api/returns/acts/available?days=7")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["acts"], "акты не показаны"
    first = body["acts"][0]
    assert first["items"] > 0 and first["matched"] > 0
    assert first["in_panel"] is False
    assert first["created_local"], "не видно, когда акт составлен"
    assert return_acts.pending() == [], "просмотр списка завёл акт"


def test_available_hides_older_acts(client, monkeypatch):
    """«За последнюю неделю» — значит старое в список не попадает."""
    from datetime import datetime, timedelta, timezone

    ozon_client = ozon.get_client(accounts.default_account())
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    monkeypatch.setattr(ozon_client, "giveout_list",
                        lambda **kw: ([{"giveout_id": 55, "giveout_status": "DONE", "created_at": old}], False))
    monkeypatch.setattr(ozon_client, "giveout_info", lambda gid: {"articles": []})
    login(client)

    assert client.get("/api/returns/acts/available?days=7").json()["acts"] == []
    assert client.get("/api/returns/acts/available?days=60").json()["acts"], "акт не показан и за 60 дней"


def test_available_marks_acts_already_in_panel(client):
    """Заведённый акт видно сразу — иначе его добавят второй раз."""
    db.execute("DELETE FROM return_acts")
    db.execute("UPDATE returns SET act_id = NULL")
    csrf = login(client)
    acts = client.get("/api/returns/acts/available?days=30").json()["acts"]
    assert not any(act["in_panel"] for act in acts), "акт помечен заведённым раньше времени"

    client.post("/api/returns/acts/import", json={"giveout_ids": [acts[0]["id"]]},
                headers={"X-CSRF-Token": csrf})
    after = client.get("/api/returns/acts/available?days=30").json()["acts"]
    taken = next(act for act in after if act["id"] == acts[0]["id"])
    assert taken["in_panel"] is True


def test_import_adds_only_the_chosen_acts(client):
    db.execute("DELETE FROM return_acts")
    db.execute("UPDATE returns SET act_id = NULL")
    csrf = login(client)
    acts = client.get("/api/returns/acts/available?days=30").json()["acts"]
    assert len(acts) >= 2, "для проверки нужно минимум два акта"
    chosen = acts[0]["id"]

    response = client.post("/api/returns/acts/import", json={"giveout_ids": [chosen]},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    assert response.json()["added"] == 1

    added = return_acts.pending()
    assert len(added) == 1, "заведено больше актов, чем выбрали"
    assert added[0]["giveout_id"] == chosen


def test_import_without_choice_is_refused(client):
    csrf = login(client)
    response = client.post("/api/returns/acts/import", json={"giveout_ids": []},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 400
    assert "не выбрано" in response.json()["detail"].lower()


def test_import_of_an_already_added_act_says_so(client):
    db.execute("DELETE FROM return_acts")
    db.execute("UPDATE returns SET act_id = NULL")
    csrf = login(client)
    chosen = client.get("/api/returns/acts/available?days=30").json()["acts"][0]["id"]
    client.post("/api/returns/acts/import", json={"giveout_ids": [chosen]},
                headers={"X-CSRF-Token": csrf})

    again = client.post("/api/returns/acts/import", json={"giveout_ids": [chosen]},
                        headers={"X-CSRF-Token": csrf}).json()
    assert again["status"] == "warning"
    assert again["added"] == 0
    assert len(return_acts.pending()) == 1


def test_available_is_admin_only(client):
    from app.security import hash_password

    db.execute(
        "INSERT INTO users(login, password_hash, role, active, created_at) "
        "VALUES('sklad4', ?, 'packer', 1, ?)",
        (hash_password("secret123"), db.now_iso()),
    )
    client.post("/login", data={"login": "sklad4", "password": "secret123", "next": "/returns"})
    assert client.get("/api/returns/acts/available").status_code == 403


def test_import_requires_csrf(client):
    login(client)
    assert client.post("/api/returns/acts/import", json={"giveout_ids": ["1"]}).status_code == 403


def test_import_is_written_to_the_log(client):
    db.execute("DELETE FROM return_acts")
    db.execute("UPDATE returns SET act_id = NULL")
    csrf = login(client)
    acts = client.get("/api/returns/acts/available?days=30").json()["acts"]
    client.post("/api/returns/acts/import", json={"giveout_ids": [acts[0]["id"]]},
                headers={"X-CSRF-Token": csrf})
    row = db.query_one("SELECT message FROM events WHERE kind = 'return_act_import'")
    assert row is not None, "добавление актов не попало в журнал"
    assert "актов: 1" in row["message"]


def test_available_explains_a_missing_method(client, monkeypatch):
    """Списка у кабинета нет — сказать это, а не показать пустую таблицу."""
    ozon_client = ozon.get_client(accounts.default_account())
    monkeypatch.setattr(ozon_client, "giveout_list",
                        lambda **kw: (_ for _ in ()).throw(OzonError("Method not found", status=404)))
    login(client)
    response = client.get("/api/returns/acts/available")
    assert response.status_code == 502
    assert "Method not found" in response.json()["detail"]
