"""Акты получения возвратов.

Акт составляет площадка: Ozon отдаёт его методами /v1/return/giveout/* со
своим составом и временем, а панель раскладывает акт на свои возвраты по
штрихкоду. Забирают в пункте всё разом — и FBS, и FBO, — поэтому один акт
закрывает поездку целиком.

Суть раздела: возврат нельзя терять с экрана в тот момент, когда его забрали.
Ozon перестаёт отдавать забранный возврат как «В пункте выдачи», и строка
исчезала ровно тогда, когда сборщик заканчивал проверку и шёл записать
результат.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app import accounts, db, giveouts, return_acts, sync
from app.main import app


@pytest.fixture
def acts(sample_data):
    """Акты, заведённые в панель.

    Какие поездки открыть — решает администратор кнопкой «Получить акты
    возвратов», поэтому в проверках заводим их явно, а не ждём от
    синхронизации.
    """
    account = accounts.default_account()
    ids = [
        row["id"] for row in db.query(
            "SELECT id FROM ozon_giveouts WHERE account_id = ?", (account["id"],)
        )
    ]
    assert ids, "подделка не отдала актов выдачи"
    giveouts.import_acts(account, {"login": "admin"}, ids)
    return return_acts.pending()


@pytest.fixture
def client(acts):
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def login(client) -> str:
    response = client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/returns"})
    assert response.status_code == 303, response.text
    page = client.get("/returns")
    return re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1)


def act_returns():
    """Возвраты, разложенные по актам площадки."""
    return {
        row["id"]: row["act_id"]
        for row in db.query("SELECT id, act_id FROM returns WHERE act_id IS NOT NULL")
    }


def a_giveout():
    row = db.query_one("SELECT * FROM ozon_giveouts ORDER BY id LIMIT 1")
    assert row is not None, "в подделке нет актов выдачи"
    return giveouts.view(row)


def mark(client, csrf, return_id, value="ok", note=""):
    response = client.post(
        "/api/returns/mark",
        json={"marketplace": "ozon", "id": return_id, "mark": value, "note": note},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text


def confirm(client, csrf, act_id):
    return client.post(f"/api/returns/acts/{act_id}/confirm", json={},
                       headers={"X-CSRF-Token": csrf})


# ---------------------------------------------------------------- акт от площадки
def test_giveout_becomes_an_act(acts):
    """Акт выдачи Ozon раскладывается в раздел «Ждёт подтверждения»."""
    acts = return_acts.pending()
    assert acts, "акты площадки не превратились в акты панели"
    assert all(act["from_ozon"] for act in acts)
    assert all(act["giveout_id"] for act in acts)


def test_act_rows_come_from_the_giveout(acts):
    """В акте ровно те возвраты, что перечислены в акте площадки."""
    giveout = a_giveout()
    act = next(a for a in return_acts.pending() if a["giveout_id"] == giveout["id"])

    barcodes = {item["barcode"] for item in giveout["items"]}
    in_act = {row["barcode"] for row in act["ozon"]}
    assert in_act == barcodes, "состав акта разошёлся с актом площадки"


def test_act_keeps_the_platform_time_and_number(acts):
    """Время выдачи берём у площадки: возвраты забирают несколько раз в день."""
    giveout = a_giveout()
    act = next(a for a in return_acts.pending() if a["giveout_id"] == giveout["id"])
    assert act["created_at"] == giveout["created_at"], "время акта не от площадки"
    assert str(giveout["id"]) in act["giveout_label"]
    assert act["giveout_status"], "статус акта площадки не сохранён"


def test_two_pickups_a_day_are_two_acts(acts):
    """Съездили дважды — два акта, каждый со своим временем."""
    acts = return_acts.pending()
    assert len(acts) >= 2, "подделка отдаёт меньше двух актов"
    times = [a["created_at"] for a in acts]
    assert len(set(times)) == len(times), "у актов одинаковое время — их не различить"
    # Свежий сверху: сборщик закрывает последнюю поездку
    assert times == sorted(times, reverse=True)


def test_fbo_returns_are_in_the_act_too(acts):
    """В пункте забирают всё разом, FBO тоже должны попадать в акт."""
    schemes = {
        row["type"] or row["scheme"]
        for row in db.query("SELECT type, scheme FROM returns WHERE act_id IS NOT NULL")
    }
    if "FBO" not in {r["type"] or r["scheme"] for r in db.query("SELECT type, scheme FROM returns")}:
        pytest.skip("в подделке нет возвратов FBO")
    assert "FBO" in schemes, "возвраты FBO не попали в акт"


def test_one_return_belongs_to_one_act(acts):
    """Две отметки на одну работу — недопустимо."""
    rows = db.query("SELECT id, act_id FROM returns WHERE act_id IS NOT NULL")
    assert len(rows) == len({row["id"] for row in rows})


def test_repeat_sync_does_not_move_returns(acts):
    before = act_returns()
    sync.sync_returns(accounts.default_account())
    assert act_returns() == before, "повторная синхронизация перетасовала акты"


def test_printing_the_sheet_does_not_create_an_act(client):
    """Акт — это факт передачи, а не намерение съездить."""
    login(client)
    before = {a["id"] for a in return_acts.pending()}
    assert client.get("/returns/print").status_code == 200
    assert {a["id"] for a in return_acts.pending()} == before


# ---------------------------------------------------------------- сопоставление
def test_matching_finds_returns_by_barcode(sample_data):
    account = accounts.default_account()
    row = db.query_one("SELECT id, barcode FROM returns WHERE barcode IS NOT NULL LIMIT 1")
    found = giveouts.match_returns(account["id"], [{"barcode": row["barcode"]}])
    assert found == [row["id"]]


def test_matching_does_not_depend_on_the_field_name(sample_data):
    """Ozon переименует колонку — сопоставление обязано пережить это.

    Поэтому ищем по значению: штрихкод узнаётся, как бы поле ни называлось.
    """
    account = accounts.default_account()
    row = db.query_one("SELECT id, barcode FROM returns WHERE barcode IS NOT NULL LIMIT 1")
    for field in ("return_barcode", "logistic_barcode", "какое_то_новое_поле"):
        assert giveouts.match_returns(account["id"], [{field: row["barcode"]}]) == [row["id"]]
    # И во вложенной структуре тоже
    assert giveouts.match_returns(
        account["id"], [{"logistic": {"barcode": row["barcode"]}}]
    ) == [row["id"]]


def test_matching_ignores_foreign_cabinets(sample_data):
    """Штрихкод чужого кабинета в наш акт попасть не должен."""
    second = accounts.get(accounts.create("ozon", "Второй Ozon", "test-client", "test-key"))
    sync.sync_returns(second)
    foreign = db.query_one(
        "SELECT barcode FROM returns WHERE account_id = ? AND barcode IS NOT NULL "
        "AND barcode NOT IN (SELECT barcode FROM returns WHERE account_id = ? AND barcode IS NOT NULL) LIMIT 1",
        (second["id"], accounts.default_account()["id"]),
    )
    if not foreign:
        pytest.skip("у кабинетов совпадают штрихкоды возвратов")
    assert giveouts.match_returns(
        accounts.default_account()["id"], [{"barcode": foreign["barcode"]}]
    ) == []


def test_act_without_recognisable_lines_is_reported(sample_data, monkeypatch):
    """Акт есть, а возвраты не опознаны — это надо сказать, а не молчать."""
    db.execute("DELETE FROM return_acts")
    db.execute("UPDATE returns SET act_id = NULL")
    account = accounts.default_account()
    from app import ozon

    client = ozon.get_client(account)
    monkeypatch.setattr(client, "giveout_list", lambda **kw: ([{"giveout_id": 1, "giveout_status": "DONE"}], False))
    monkeypatch.setattr(client, "giveout_info", lambda gid: {"articles": [{"article_name": "Неизвестно"}]})

    result = giveouts.sync_account(account)
    assert result["giveouts"] == 1
    assert result.get("giveouts_unmatched") == 1
    assert result.get("giveouts_returns") is None
    assert return_acts.pending() == [], "акт без опознанных строк показывать нечего"


# ---------------------------------------------------------------- запасной путь
def test_return_taken_before_the_act_is_not_lost(sample_data):
    """Возврат пропал из выдачи, акта площадки ещё нет — он не должен исчезнуть."""
    db.execute("DELETE FROM return_acts")
    db.execute("UPDATE returns SET act_id = NULL")
    account = accounts.default_account()
    from app import ozon

    client = ozon.get_client(account)
    target = db.query_one("SELECT id FROM returns WHERE is_ready = 1 LIMIT 1")["id"]
    original_returns, original_giveouts = client.returns_list, client.giveout_list
    client.returns_list = lambda *a, **kw: (
        [r for r in original_returns(*a, **kw)[0] if str(r.get("id")) != str(target)],
        original_returns(*a, **kw)[1],
    )
    client.giveout_list = lambda **kw: ([], False)
    sync.sync_returns(account)
    client.returns_list, client.giveout_list = original_returns, original_giveouts

    acts = return_acts.pending()
    assert acts, "возврат пропал молча"
    assert acts[0]["no_sheet"] is True
    assert [r["id"] for r in acts[0]["ozon"]] == [target]


def test_added_act_takes_the_return_over(sample_data):
    """Завели акт площадки — возврат переезжает в него из запасного акта."""
    account = accounts.default_account()
    db.execute("DELETE FROM return_acts")
    db.execute("UPDATE returns SET act_id = NULL")

    giveout = a_giveout()
    target = db.query_one(
        "SELECT id FROM returns WHERE barcode = ?", (giveout["items"][0]["barcode"],)
    )["id"]
    spare = return_acts.collect_orphans(account["id"], [target])
    assert return_acts.detail(spare)["no_sheet"] is True

    giveouts.import_acts(account, {"login": "admin"}, [giveout["id"]])

    moved = db.query_one("SELECT act_id FROM returns WHERE id = ?", (target,))["act_id"]
    assert moved != spare, "возврат остался в запасном акте"
    assert return_acts.get(moved)["giveout_id"] == giveout["id"]
    assert return_acts.get(spare) is None, "опустевший запасной акт не убран"


def test_confirmed_act_is_not_reshuffled(acts):
    """Подтверждённый акт трогать нельзя: работа по нему уже закрыта."""
    account = accounts.default_account()
    act = return_acts.pending()[0]
    for row in act["ozon"]:
        db.execute("UPDATE returns SET mark = 'ok' WHERE id = ?", (row["id"],))
    return_acts.confirm(act["id"], {"login": "admin"})

    giveouts.sync_account(account)
    still = {row["id"] for row in return_acts.detail(act["id"])["ozon"]}
    assert still == {row["id"] for row in act["ozon"]}, "подтверждённый акт изменился"


# ---------------------------------------------------------------- подтверждение
def test_act_cannot_be_confirmed_while_something_is_unmarked(client):
    csrf = login(client)
    act = return_acts.pending()[0]

    response = confirm(client, csrf, act["id"])
    assert response.status_code == 409
    assert "отметьте" in response.json()["detail"]
    assert any(a["id"] == act["id"] for a in return_acts.pending()), "акт закрылся без отметок"


def test_fully_marked_act_is_confirmed(client):
    csrf = login(client)
    act = return_acts.pending()[0]
    for row in act["ozon"]:
        mark(client, csrf, row["id"], "ok", "цел")

    assert return_acts.detail(act["id"])["can_confirm"] is True
    assert confirm(client, csrf, act["id"]).status_code == 200
    assert not any(a["id"] == act["id"] for a in return_acts.pending())

    stored = return_acts.get(act["id"])
    assert stored["confirmed_by"] == "admin" and stored["confirmed_at"]


def test_confirmation_is_written_to_the_log(client):
    csrf = login(client)
    act = return_acts.pending()[0]
    for row in act["ozon"]:
        mark(client, csrf, row["id"], "ok")
    confirm(client, csrf, act["id"])

    row = db.query_one("SELECT message FROM events WHERE kind = 'return_act_confirm'")
    assert row is not None, "подтверждение акта не попало в журнал"
    assert "принято" in row["message"]


def test_act_confirmed_twice_says_so(client):
    csrf = login(client)
    act = return_acts.pending()[0]
    for row in act["ozon"]:
        mark(client, csrf, row["id"], "ok")
    confirm(client, csrf, act["id"])

    again = confirm(client, csrf, act["id"])
    assert again.status_code == 200
    assert again.json()["status"] == "warning"
    assert "уже подтвердил" in again.json()["message"]


def test_confirm_requires_csrf(client):
    login(client)
    act = return_acts.pending()[0]
    assert client.post(f"/api/returns/acts/{act['id']}/confirm", json={}).status_code == 403


def test_unknown_act_is_404(client):
    csrf = login(client)
    assert confirm(client, csrf, "нет-такого").status_code == 404


# ---------------------------------------------------------------- на экране
def test_page_shows_the_acts(client):
    login(client)
    page = client.get("/returns?tab=acts")
    assert page.status_code == 200
    assert "Ждёт подтверждения" in page.text
    for act in return_acts.pending():
        for row in act["ozon"]:
            assert str(row["id"]) in page.text, f"возврата {row['id']} нет во вкладке"
        assert str(act["giveout_id"]) in page.text, "не видно, из какого акта Ozon строки"


def test_main_tab_is_unchanged(client):
    """Главная страница возвратов показывает только то, что лежит в ПВЗ."""
    login(client)
    page = client.get("/returns")
    assert page.status_code == 200
    ready = [row["id"] for row in db.query("SELECT id FROM returns WHERE is_ready = 1")]
    for return_id in ready:
        assert str(return_id) in page.text


def test_header_counts_open_acts(client):
    login(client)
    page = client.get("/returns")
    assert "Акты ждут подтверждения" in page.text


# ---------------------------------------------------------------- печать и PDF акта
def test_act_sheet_shows_the_marks(client):
    csrf = login(client)
    act = return_acts.pending()[0]
    mark(client, csrf, act["ozon"][0]["id"], "bad", "вскрыта упаковка")

    page = client.get(f"/returns/acts/{act['id']}/print")
    assert page.status_code == 200
    assert "Акт ·" in page.text
    assert "вскрыта упаковка" in page.text
    assert "✗ Не принят" in page.text


def test_act_pdf_is_a_pdf(client):
    csrf = login(client)
    act = return_acts.pending()[0]
    mark(client, csrf, act["ozon"][0]["id"], "ok", "всё на месте")

    response = client.get(f"/returns/acts/{act['id']}.pdf")
    assert response.status_code == 200, response.text
    assert response.content[:5] == b"%PDF-"

    from io import BytesIO
    from pypdf import PdfReader

    text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(response.content)).pages)
    assert "всё на месте" in text and "Принят" in text


def test_act_pdf_of_unknown_act_is_404(client):
    login(client)
    assert client.get("/returns/acts/нет-такого.pdf").status_code == 404
