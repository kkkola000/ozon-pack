"""Акты получения возвратов.

Акт — одна поездка в пункт выдачи. Составляет его панель, а не площадка:
актов о возвратах Ozon не отдаёт. Признак получения — статус возврата
«Получен» (ReceivedBySeller): он означает, что возврат уже у нас, и по нему
нужна отметка.

Суть раздела: возврат нельзя терять с экрана в тот момент, когда его забрали.
Ozon перестаёт отдавать забранный возврат как «В пункте выдачи», и строка
исчезала ровно тогда, когда сборщик заканчивал проверку и шёл записать
результат.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app import accounts, db, options, return_acts, store, sync
from app.main import app


def take_everything(account=None):
    """Съездить за возвратами: всё из пункта выдачи переходит в «Получен».

    Дальше обновление само заметит перемену статуса и соберёт акт — ровно так
    это происходит в жизни, без единого действия человека в панели.
    """
    from app import ozon

    account = account or accounts.default_account()
    taken = ozon.get_client(account).receive()
    sync.sync_returns(account)
    return taken


@pytest.fixture
def acts(sample_data):
    """Возвраты забрали, панель это заметила."""
    take_everything()
    pending = return_acts.pending()
    assert pending, "полученные возвраты не собрались в акт"
    return pending


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
    """Возвраты, разложенные по актам."""
    return {
        row["id"]: row["act_id"]
        for row in db.query("SELECT id, act_id FROM returns WHERE act_id IS NOT NULL")
    }


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


def mark_all(act):
    for row in act["ozon"] + act["avito"]:
        db.execute("UPDATE returns SET mark = 'ok' WHERE id = ?", (row["id"],))
        db.execute("UPDATE avito_orders SET mark = 'ok' WHERE id = ?", (row["id"],))


# ------------------------------------------------- акт собирается по статусу
def test_received_return_becomes_an_act(sample_data):
    """Возврат перешёл в «Получен» — панель сама завела акт на подтверждение."""
    taken = take_everything()
    assert taken, "подделка не отдала ни одного возврата из пункта выдачи"

    acts = return_acts.pending()
    assert len(acts) == 1, "полученное должно быть одним актом, а не россыпью"
    assert acts[0]["auto"] is True
    assert {str(row["id"]) for row in acts[0]["ozon"]} >= set(taken)


def test_one_act_covers_all_the_items(sample_data):
    """Съездили один раз — акт один, по всем товарам сразу."""
    take_everything()
    acts = return_acts.pending()
    assert len(acts) == 1
    received = db.query_one(
        "SELECT COUNT(*) AS c FROM returns WHERE received_at IS NOT NULL"
    )["c"]
    assert acts[0]["total"] == received, "не все полученные попали в один акт"


def test_act_is_dated_by_the_moment_it_was_formed(sample_data):
    """Акт за текущие дату и время: за возвратами ездят несколько раз в день."""
    before = db.now_iso()
    take_everything()
    act = return_acts.pending()[0]
    assert before <= act["created_at"] <= db.now_iso()
    assert act["title"].startswith("Возвраты за ")
    assert act["created_local"] in act["title"]


def test_fbo_returns_are_in_the_act_too(sample_data):
    """В пункте забирают всё разом, FBO тоже должны попадать в акт."""
    take_everything()
    all_schemes = {r["type"] or r["scheme"] for r in db.query("SELECT type, scheme FROM returns")}
    if "FBO" not in all_schemes:
        pytest.skip("в подделке нет возвратов FBO")
    in_acts = {
        row["type"] or row["scheme"]
        for row in db.query("SELECT type, scheme FROM returns WHERE act_id IS NOT NULL")
    }
    assert "FBO" in in_acts, "возвраты FBO не попали в акт"


def test_received_returns_leave_the_pickup_list(sample_data):
    """Полученный возврат — уже не «к выдаче»: сборщику за ним ехать не надо."""
    taken = take_everything()
    placeholders = ",".join("?" for _ in taken)
    rows = db.query(f"SELECT id, is_ready FROM returns WHERE id IN ({placeholders})", taken)
    assert rows and all(row["is_ready"] == 0 for row in rows)


def test_a_second_trip_is_a_second_act(sample_data, monkeypatch):
    """Две поездки в разное время — два акта: подписывают их отдельно."""
    from app import ozon

    account = accounts.default_account()
    client = ozon.get_client(account)
    first = db.query_one("SELECT id FROM returns WHERE is_ready = 1 LIMIT 1")["id"]
    client.receive(first)
    sync.sync_returns(account)
    assert len(return_acts.pending()) == 1

    # Вторая поездка позже: акт, собранный час назад, для неё уже закрыт.
    old = db.now_iso()
    db.execute("UPDATE return_acts SET created_at = datetime(?, '-3 hours')", (old,))
    client.receive()
    sync.sync_returns(account)

    acts = return_acts.pending()
    assert len(acts) == 2, "вторая поездка попала в акт первой"
    assert acts[0]["created_at"] > acts[1]["created_at"], "свежий акт должен быть сверху"


def test_returns_of_one_trip_do_not_split(sample_data, monkeypatch):
    """Площадка провела часть возвратов позже — акт всё равно один.

    Статусы меняются не одномоментно, а обновление идёт раз в несколько минут.
    Без этого одна поездка разошлась бы по нескольким актам.
    """
    from app import ozon

    account = accounts.default_account()
    client = ozon.get_client(account)
    ready = [row["id"] for row in db.query("SELECT id FROM returns WHERE is_ready = 1")]
    assert len(ready) >= 2

    client.receive(ready[0])
    sync.sync_returns(account)
    client.receive(*ready[1:])
    sync.sync_returns(account)

    acts = return_acts.pending()
    assert len(acts) == 1, "одна поездка разошлась по нескольким актам"
    assert acts[0]["total"] >= len(ready)


# --------------------------------------------- защита от повторной загрузки
def test_repeat_sync_does_not_move_returns(acts):
    before = act_returns()
    sync.sync_returns(accounts.default_account())
    assert act_returns() == before, "повторная синхронизация перетасовала акты"


def test_return_still_reported_as_received_is_not_taken_twice(acts):
    """Ozon отдаёт «Получен» и дальше — второго акта на тот же возврат нет."""
    act = return_acts.pending()[0]
    before = {row["id"] for row in act["ozon"]}
    for _ in range(3):
        sync.sync_returns(accounts.default_account())
    assert len(return_acts.pending()) == 1, "повтор статуса завёл лишний акт"
    assert {row["id"] for row in return_acts.pending()[0]["ozon"]} == before


def test_moment_of_receipt_is_written_once(acts):
    """Дата получения не переписывается: иначе возврат уехал бы в новый акт."""
    row = db.query_one("SELECT id, received_at FROM returns WHERE received_at IS NOT NULL LIMIT 1")
    sync.sync_returns(accounts.default_account())
    again = db.query_one("SELECT received_at FROM returns WHERE id = ?", (row["id"],))
    assert again["received_at"] == row["received_at"]


def test_return_of_a_confirmed_act_is_never_taken_again(acts):
    """Акт подтвердили — работа закрыта, и возврат в новый акт не попадёт."""
    act = return_acts.pending()[0]
    mark_all(act)
    return_acts.confirm(act["id"], {"login": "admin"})

    sync.sync_returns(accounts.default_account())
    assert return_acts.pending() == [], "подтверждённые возвраты собрались заново"
    still = {row["act_id"] for row in db.query(
        "SELECT act_id FROM returns WHERE id IN (SELECT id FROM returns WHERE act_id IS NOT NULL)"
    )}
    assert still == {act["id"]}


# ----------------------------------------------------- загрузка за число
def test_loading_a_day_makes_one_act(sample_data):
    """Полученные за указанное число собираются в один акт."""
    from app import ozon

    account = accounts.default_account()
    ozon.get_client(account).receive()
    # Возвраты уже полученные, но акта на них нет: так выглядит база, если
    # обновление в тот день не отработало.
    sync.sync_returns(account)
    db.execute("DELETE FROM return_acts")
    db.execute("UPDATE returns SET act_id = NULL")

    day = store.local_day()
    result = return_acts.from_received(account["id"], user={"login": "admin"}, day=day)
    assert result["status"] == "ok"
    acts = return_acts.pending()
    assert len(acts) == 1 and acts[0]["by_day"] is True
    assert acts[0]["received_day"] == day
    assert day.replace("-", ".") not in acts[0]["title"]  # число показываем по-русски
    assert acts[0]["total"] == result["added"]


def test_loading_the_same_day_twice_adds_nothing(sample_data):
    """Второй раз то же число — второго акта и вторых отметок не будет."""
    account = accounts.default_account()
    take_everything()
    db.execute("DELETE FROM return_acts")
    db.execute("UPDATE returns SET act_id = NULL")

    day = store.local_day()
    first = return_acts.from_received(account["id"], user={"login": "admin"}, day=day)
    again = return_acts.from_received(account["id"], user={"login": "admin"}, day=day)

    assert again["added"] == 0 and again["status"] == "warning"
    assert len(return_acts.pending()) == 1
    assert return_acts.pending()[0]["id"] == first["act_id"]


def test_loading_a_day_without_receipts_says_so(sample_data):
    account = accounts.default_account()
    result = return_acts.from_received(account["id"], user={"login": "admin"}, day="2001-01-01")
    assert result["status"] == "warning"
    assert result["act_id"] is None
    assert "01.01.2001" in result["message"]


def login_as_packer(client) -> str:
    from app.security import hash_password

    db.execute(
        "INSERT INTO users(login, password_hash, role, active, created_at) VALUES(?,?,?,1,?)",
        ("packer9", hash_password("packer123456"), "packer", db.now_iso()),
    )
    client.post("/login", data={"login": "packer9", "password": "packer123456", "next": "/returns"})
    page = client.get("/returns")
    return re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1)


def test_day_endpoint_is_admin_only(client):
    """Акт нельзя удалить — заводить его может только администратор."""
    csrf = login_as_packer(client)
    response = client.post("/api/returns/acts/by-day", json={"day": store.local_day()},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 403


def test_day_endpoint_previews_before_creating(client):
    csrf = login(client)
    db.execute("DELETE FROM return_acts")
    db.execute("UPDATE returns SET act_id = NULL")

    preview = client.post("/api/returns/acts/by-day",
                          json={"day": store.local_day(), "dry_run": True},
                          headers={"X-CSRF-Token": csrf})
    assert preview.status_code == 200, preview.text
    assert preview.json()["found"] > 0
    assert return_acts.pending() == [], "предпросмотр завёл акт"

    created = client.post("/api/returns/acts/by-day", json={"day": store.local_day()},
                          headers={"X-CSRF-Token": csrf})
    assert created.status_code == 200, created.text
    assert created.json()["act_id"]
    assert len(return_acts.pending()) == 1


def test_day_endpoint_rejects_a_broken_date(client):
    csrf = login(client)
    response = client.post("/api/returns/acts/by-day", json={"day": "вчера"},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 400
    assert "ГГГГ-ММ-ДД" in response.json()["detail"]


def test_day_endpoint_requires_csrf(client):
    login(client)
    assert client.post("/api/returns/acts/by-day", json={"day": store.local_day()}).status_code == 403


# ---------------------------------------------------------------- запасной путь
def test_return_gone_without_the_received_status_is_not_lost(sample_data):
    """Возврат пропал из выдачи, «Получен» по нему не приходил."""
    db.execute("DELETE FROM return_acts")
    db.execute("UPDATE returns SET act_id = NULL, received_at = NULL, received_day = NULL")
    account = accounts.default_account()
    from app import ozon

    client = ozon.get_client(account)
    target = db.query_one("SELECT id FROM returns WHERE is_ready = 1 LIMIT 1")["id"]
    original = client.returns_list
    client.returns_list = lambda *a, **kw: (
        [r for r in original(*a, **kw)[0] if str(r.get("id")) != str(target)],
        original(*a, **kw)[1],
    )
    sync.sync_returns(account)
    client.returns_list = original

    acts = return_acts.pending()
    assert acts, "возврат пропал молча"
    spare = next(a for a in acts if a["no_sheet"])
    assert [r["id"] for r in spare["ozon"]] == [target]


def test_received_status_takes_the_return_over(sample_data):
    """Пришёл «Получен» — возврат переезжает из запасного акта в акт получения."""
    account = accounts.default_account()
    db.execute("DELETE FROM return_acts")
    db.execute("UPDATE returns SET act_id = NULL, received_at = NULL, received_day = NULL")

    target = db.query_one("SELECT id FROM returns WHERE is_ready = 1 LIMIT 1")["id"]
    spare = return_acts.collect_orphans(account["id"], [target])
    assert return_acts.detail(spare)["no_sheet"] is True

    take_everything(account)

    moved = db.query_one("SELECT act_id FROM returns WHERE id = ?", (target,))["act_id"]
    assert moved != spare, "возврат остался в запасном акте"
    assert return_acts.get(moved)["kind"] == return_acts.RECEIVED
    assert return_acts.get(spare) is None, "опустевший запасной акт не убран"


# ------------------------------------------------------------------ настройка
def test_received_statuses_are_a_separate_setting(sample_data):
    """Списки «к выдаче» и «получен» не должны быть одним списком.

    Иначе включить акты значило бы показать сборщику то, за чем ехать уже не
    надо, — а выключить показ значило бы потерять акты.
    """
    assert options.get_returns_statuses() == ["ArrivedAtReturnPlace"]
    assert options.get_received_statuses() == ["ReceivedBySeller"]
    assert set(options.wanted_statuses()) == {"ArrivedAtReturnPlace", "ReceivedBySeller"}


def test_status_in_both_lists_stays_on_the_pickup_list(sample_data):
    """За таким возвратом ещё едут: закрывать актом неполученное нельзя."""
    options.set_received_statuses(["ArrivedAtReturnPlace", "ReceivedBySeller"])
    db.execute("DELETE FROM return_acts")
    db.execute("UPDATE returns SET act_id = NULL, received_at = NULL, received_day = NULL")
    sync.sync_returns(accounts.default_account())

    ready = db.query("SELECT received_at FROM returns WHERE is_ready = 1")
    assert ready and all(row["received_at"] is None for row in ready)


def test_empty_received_list_turns_the_acts_off(sample_data):
    options.set_received_statuses([])
    db.execute("DELETE FROM return_acts")
    db.execute("UPDATE returns SET act_id = NULL, received_at = NULL, received_day = NULL")
    take_everything()
    assert db.query_one("SELECT COUNT(*) AS c FROM returns WHERE received_at IS NOT NULL")["c"] == 0


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


# ------------------------------------------------------- подтверждённое в отчёты
def test_confirmed_act_moves_to_reports(client):
    csrf = login(client)
    act = return_acts.pending()[0]
    for row in act["ozon"]:
        mark(client, csrf, row["id"], "ok", "цел")
    assert confirm(client, csrf, act["id"]).status_code == 200

    page = client.get("/reports/returns")
    assert page.status_code == 200
    assert act["title"] in page.text
    assert "admin" in page.text


def test_report_act_page_shows_marks_and_notes(client):
    csrf = login(client)
    act = return_acts.pending()[0]
    for row in act["ozon"]:
        mark(client, csrf, row["id"], "ok", "цел")
    mark(client, csrf, act["ozon"][0]["id"], "bad", "вскрыта упаковка")
    confirm(client, csrf, act["id"])

    page = client.get(f"/reports/returns/{act['id']}")
    assert page.status_code == 200
    assert "вскрыта упаковка" in page.text
    assert "Не принят" in page.text
    for row in act["ozon"]:
        assert str(row["id"]) in page.text


def test_reports_returns_is_admin_only(client):
    """В актах видны отметки и комментарии всех сборщиков — это для админа."""
    login_as_packer(client)
    assert client.get("/reports/returns").status_code == 403
    assert client.get("/reports/returns/что-нибудь").status_code == 403


def test_unconfirmed_act_is_not_in_reports(client):
    login(client)
    act = return_acts.pending()[0]
    page = client.get("/reports/returns")
    assert page.status_code == 200
    assert act["id"] not in page.text


def test_reports_day_route_still_works(client):
    """«returns» не должно уезжать в разбор даты — иначе раздел даёт 404."""
    login(client)
    assert client.get("/reports/2026-01-05").status_code == 200
    assert client.get("/reports/не-дата").status_code == 404


# ---------------------------------------------------------------- на экране
def test_page_shows_the_acts(client):
    login(client)
    page = client.get("/returns?tab=acts")
    assert page.status_code == 200
    assert "Ждёт подтверждения" in page.text
    for act in return_acts.pending():
        for row in act["ozon"]:
            assert str(row["id"]) in page.text, f"возврата {row['id']} нет во вкладке"


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


def test_printing_the_sheet_does_not_create_an_act(client):
    """Акт — это факт получения, а не намерение съездить."""
    login(client)
    before = {a["id"] for a in return_acts.pending()}
    assert client.get("/returns/print").status_code == 200
    assert {a["id"] for a in return_acts.pending()} == before


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
