"""Акты получения возвратов.

Суть: возврат нельзя терять с экрана в тот момент, когда его забрали. Раньше
Ozon переставал отдавать забранный возврат, и строка исчезала ровно тогда,
когда сборщик заканчивал проверку и шёл записать результат. Акт держит её до
подтверждения.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app import accounts, avito, db, return_acts, sync
from app.main import app


@pytest.fixture
def client(sample_data):
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def login(client) -> str:
    response = client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/returns"})
    assert response.status_code == 303, response.text
    page = client.get("/returns")
    return re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1)


def ready_ids(account_id=None):
    account_id = account_id or accounts.default_account()["id"]
    return [
        row["id"] for row in db.query(
            "SELECT id FROM returns WHERE account_id = ? AND is_ready = 1 ORDER BY id", (account_id,)
        )
    ]


def pickup(ids):
    """Возвраты забрали: Ozon перестал отдавать их как «В пункте выдачи»."""
    account = accounts.default_account()
    from app import ozon

    client = ozon.get_client(account)
    original = client.returns_list

    def without(*a, **kw):
        returns, has_next = original(*a, **kw)
        return [r for r in returns if str(r.get("id")) not in set(map(str, ids))], has_next

    client.returns_list = without
    sync.sync_returns(account)
    client.returns_list = original


def mark(client, csrf, return_id, value="ok", note="", marketplace="ozon"):
    response = client.post(
        "/api/returns/mark",
        json={"marketplace": marketplace, "id": return_id, "mark": value, "note": note},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    return response


# ---------------------------------------------------------------- акт заводится печатью
def test_printing_the_sheet_opens_an_act(client):
    login(client)
    assert return_acts.pending() == []

    assert client.get("/returns/print").status_code == 200

    acts = return_acts.pending()
    assert len(acts) == 1, "печать листа не завела акт"
    act = acts[0]
    assert act["total"] == len(ready_ids())
    assert act["created_by"] == "admin"
    assert act["unmarked"] == act["total"]
    assert act["can_confirm"] is False, "пустой акт нельзя подтверждать"


def test_pdf_sheet_opens_an_act_too(client):
    """Лист скачали файлом — за ним поедут так же, как за напечатанным."""
    login(client)
    assert client.get("/returns/sheet.pdf").status_code == 200
    assert len(return_acts.pending()) == 1


def test_reprinting_does_not_move_returns_to_a_new_act(client):
    """Повторная печать не переписывает прошлую поездку задним числом."""
    login(client)
    client.get("/returns/print")
    first = return_acts.pending()[0]

    client.get("/returns/print")
    acts = return_acts.pending()
    assert len(acts) == 1, "перепечатка того же листа завела лишний акт"
    assert acts[0]["id"] == first["id"]
    assert acts[0]["total"] == first["total"]


def test_new_returns_go_to_a_new_act(client):
    """Появился новый возврат — печать заводит второй акт только на него."""
    login(client)
    client.get("/returns/print")
    first = return_acts.pending()[0]

    db.execute(
        "INSERT INTO returns(account_id, id, type, status_sys, status_name, product_name, quantity,"
        " is_ready, first_seen_at, updated_at)"
        " VALUES(?, 'new-1', 'FBS', 'ArrivedAtReturnPlace', 'В пункте выдачи', 'Новый возврат', 1, 1, ?, ?)",
        (accounts.default_account()["id"], db.now_iso(), db.now_iso()),
    )
    client.get("/returns/print")

    acts = {a["id"]: a for a in return_acts.pending()}
    assert len(acts) == 2
    assert acts[first["id"]]["total"] == first["total"], "старый акт изменился"
    fresh = next(a for a in acts.values() if a["id"] != first["id"])
    assert [r["id"] for r in fresh["ozon"]] == ["new-1"]


# ---------------------------------------------------------------- возврат не теряется
def test_received_return_stays_in_the_act(client):
    """Главное свойство: забрали возврат — строка осталась в акте."""
    login(client)
    client.get("/returns/print")
    ids = ready_ids()

    pickup(ids[:2])

    assert ready_ids() == ids[2:], "возвраты не ушли из списка к выдаче"
    act = return_acts.pending()[0]
    assert {r["id"] for r in act["ozon"]} >= set(ids[:2]), "забранные возвраты выпали из акта"
    assert act["received"] == 2


def test_page_shows_the_act_with_received_returns(client):
    login(client)
    client.get("/returns/print")
    ids = ready_ids()
    pickup(ids)

    page = client.get("/returns?tab=acts")
    assert page.status_code == 200
    assert "Ждёт подтверждения" in page.text
    for return_id in ids:
        assert str(return_id) in page.text, f"возврата {return_id} нет во вкладке актов"


def test_main_tab_is_unchanged(client):
    """Главная страница возвратов осталась прежней — там только то, что к выдаче."""
    login(client)
    client.get("/returns/print")
    ids = ready_ids()
    pickup(ids[:1])

    page = client.get("/returns")
    assert page.status_code == 200
    assert str(ids[0]) not in page.text, "забранный возврат остался в списке к выдаче"
    for return_id in ids[1:]:
        assert str(return_id) in page.text


def test_return_taken_without_a_sheet_lands_in_its_own_act(client):
    """За возвратом съездили без листа — он всё равно должен ждать отметки."""
    login(client)
    ids = ready_ids()
    pickup(ids[:1])

    acts = return_acts.pending()
    assert len(acts) == 1, "возврат без листа пропал молча"
    assert acts[0]["no_sheet"] is True
    assert [r["id"] for r in acts[0]["ozon"]] == [ids[0]]


def test_sync_keeps_marked_returns_when_statuses_change(client):
    """Смена списка статусов не должна стирать отметки вместе со строками."""
    from app import options

    csrf = login(client)
    client.get("/returns/print")
    target = ready_ids()[0]
    mark(client, csrf, target, "bad", "нет комплекта")

    options.set_returns_statuses(["MovingToSeller"])
    sync.sync_returns(accounts.default_account())

    row = db.query_one("SELECT mark, note FROM returns WHERE id = ?", (target,))
    assert row is not None, "строка с отметкой удалена при смене статусов"
    assert row["mark"] == "bad" and row["note"] == "нет комплекта"


# ---------------------------------------------------------------- подтверждение
def confirm(client, csrf, act_id):
    return client.post(f"/api/returns/acts/{act_id}/confirm", json={},
                       headers={"X-CSRF-Token": csrf})


def test_act_cannot_be_confirmed_while_something_is_unmarked(client):
    csrf = login(client)
    client.get("/returns/print")
    act = return_acts.pending()[0]

    response = confirm(client, csrf, act["id"])
    assert response.status_code == 409
    assert "отметьте" in response.json()["detail"]
    assert len(return_acts.pending()) == 1, "акт закрылся без отметок"


def test_fully_marked_act_is_confirmed(client):
    csrf = login(client)
    client.get("/returns/print")
    act = return_acts.pending()[0]
    for row in act["ozon"]:
        mark(client, csrf, row["id"], "ok", "цел")

    assert return_acts.detail(act["id"])["can_confirm"] is True
    response = confirm(client, csrf, act["id"])
    assert response.status_code == 200, response.text
    assert return_acts.pending() == [], "подтверждённый акт остался в списке"

    stored = return_acts.get(act["id"])
    assert stored["confirmed_by"] == "admin" and stored["confirmed_at"]


def test_confirmation_is_written_to_the_log(client):
    csrf = login(client)
    client.get("/returns/print")
    act = return_acts.pending()[0]
    for row in act["ozon"]:
        mark(client, csrf, row["id"], "ok")
    confirm(client, csrf, act["id"])

    row = db.query_one("SELECT message FROM events WHERE kind = 'return_act_confirm'")
    assert row is not None, "подтверждение акта не попало в журнал"
    assert "принято" in row["message"]


def test_act_confirmed_twice_says_so(client):
    csrf = login(client)
    client.get("/returns/print")
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
    client.get("/returns/print")
    act = return_acts.pending()[0]
    assert client.post(f"/api/returns/acts/{act['id']}/confirm", json={}).status_code == 403


def test_unknown_act_is_404(client):
    csrf = login(client)
    assert confirm(client, csrf, "нет-такого").status_code == 404


# ---------------------------------------------------------------- печать и PDF акта
def test_act_sheet_shows_the_marks(client):
    csrf = login(client)
    client.get("/returns/print")
    act = return_acts.pending()[0]
    mark(client, csrf, act["ozon"][0]["id"], "bad", "вскрыта упаковка")

    page = client.get(f"/returns/acts/{act['id']}/print")
    assert page.status_code == 200
    assert "Акт ·" in page.text
    assert "вскрыта упаковка" in page.text
    assert "✗ Не принят" in page.text


def test_act_pdf_is_a_pdf(client):
    csrf = login(client)
    client.get("/returns/print")
    act = return_acts.pending()[0]
    mark(client, csrf, act["ozon"][0]["id"], "ok", "всё на месте")

    response = client.get(f"/returns/acts/{act['id']}.pdf")
    assert response.status_code == 200, response.text
    assert response.content[:5] == b"%PDF-"
    assert "attachment" in response.headers["content-disposition"]

    from io import BytesIO
    from pypdf import PdfReader

    text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(response.content)).pages)
    assert "всё на месте" in text
    assert "Принят" in text


def test_act_pdf_of_unknown_act_is_404(client):
    login(client)
    assert client.get("/returns/acts/нет-такого.pdf").status_code == 404


# ---------------------------------------------------------------- счётчик в шапке
def test_header_shows_open_acts(client):
    login(client)
    client.get("/returns/print")
    page = client.get("/returns")
    assert "Акты ждут подтверждения" in page.text


def test_header_is_quiet_without_acts(client):
    login(client)
    page = client.get("/returns")
    assert "Акты ждут подтверждения" not in page.text


# ---------------------------------------------------------------- акт по всем кабинетам
def test_all_cabinets_sheet_makes_one_act_for_both_marketplaces(client):
    second = accounts.get(accounts.create("avito", "Кабинет Avito", "test-client", "test-secret"))
    sync.sync_avito(second)
    login(client)

    assert client.get("/returns/print?scope=all").status_code == 200
    acts = return_acts.pending()
    assert len(acts) == 1, "лист по всем кабинетам должен давать один акт"
    act = acts[0]
    assert act["kind"] == "all"
    assert act["ozon"], "в акте нет возвратов Ozon"
    on_return = db.query_one(
        "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND status = ?",
        (second["id"], avito.STATUS_ON_RETURN),
    )["c"]
    if on_return:
        assert act["avito"], "в акте нет заказов Avito"


def test_shared_act_is_visible_from_any_cabinet(client):
    """Лист был общий — подтверждать его логично там же, где на него смотрят."""
    second = accounts.get(accounts.create("avito", "Кабинет Avito", "test-client", "test-secret"))
    sync.sync_avito(second)
    csrf = login(client)
    client.get("/returns/print?scope=all")

    switched = client.post("/api/account/switch", json={"account_id": second["id"], "next": "/avito"},
                           headers={"X-CSRF-Token": csrf})
    assert switched.status_code == 200, switched.text
    assert len(return_acts.pending([second["id"]])) == 1
