"""Акты получения возвратов.

Акт — одна поездка в пункт выдачи. Составляет его человек за выбранное число:
панель не знает, когда поездка закончилась. Признак получения — статус
возврата «Получен» (ReceivedBySeller): он означает, что возврат уже у нас, и
по нему нужна отметка.

За возвратами ездят несколько раз в день, поэтому актов за одно число бывает
несколько. Главное, что здесь проверяется: возврат не может попасть в два акта
ни при каком порядке действий — иначе по одной работе будет две отметки.

Суть раздела: возврат нельзя терять с экрана в тот момент, когда его забрали.
Ozon перестаёт отдавать забранный возврат как «В пункте выдачи», и строка
исчезала ровно тогда, когда сборщик заканчивал проверку и шёл записать
результат.
"""
import re

import pytest
from fastapi.testclient import TestClient

from datetime import datetime, timedelta

from app import accounts, db, options, return_acts, store, sync
from app.main import app


def take_everything(account=None):
    """Съездить за возвратами: всё из пункта выдачи переходит в «Получен».

    Обновление это заметит и запишет момент получения, но акта не составит:
    акт — решение человека.
    """
    from app import ozon

    account = account or accounts.default_account()
    taken = ozon.get_client(account).receive()
    sync.sync_returns(account)
    return taken


def make_act(account=None, day=None, user=None):
    """Составить акт за число — то же, что нажать кнопку во вкладке."""
    account = account or accounts.default_account()
    return return_acts.from_received(
        account["id"], day or store.local_day(), user=user or {"login": "admin"}
    )


@pytest.fixture
def acts(sample_data):
    """Возвраты забрали и свели в акт."""
    take_everything()
    assert make_act()["status"] == "ok"
    pending = return_acts.pending()
    assert pending, "полученные возвраты не попали в акт"
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


# ------------------------------------------------- акт составляет человек
def test_sync_does_not_make_acts(sample_data):
    """Обновление акта не составляет: когда поездка кончилась, знает человек."""
    taken = take_everything()
    assert taken, "подделка не отдала ни одного возврата из пункта выдачи"
    assert return_acts.pending() == [], "обновление составило акт само"
    # Но получение записано — акт будет из чего составить.
    free = return_acts.received_returns(accounts.default_account()["id"], store.local_day())
    assert set(free) >= set(taken)


def test_act_takes_everything_received_that_day(sample_data):
    """Один акт — все свободные полученные возвраты выбранного числа."""
    take_everything()
    result = make_act()
    acts = return_acts.pending()
    assert len(acts) == 1 and acts[0]["by_day"] is True
    assert acts[0]["received_day"] == store.local_day()
    assert acts[0]["total"] == result["added"]
    assert not return_acts.received_returns(accounts.default_account()["id"], store.local_day())


def test_act_title_names_the_day_the_number_and_the_time(sample_data):
    """За одно число актов несколько — по заголовку их надо различать."""
    take_everything()
    make_act()
    act = return_acts.pending()[0]
    assert act["title"].startswith("Возвраты за ")
    assert store.local_time(act["created_at"], "%d.%m.%Y") in act["title"]
    assert store.local_time(act["created_at"], "%H:%M") in act["title"]
    assert "№1" in act["title"], "первый акт числа должен быть номером 1"


def test_fbo_returns_are_in_the_act_too(sample_data):
    """В пункте забирают всё разом, FBO тоже должны попадать в акт."""
    take_everything()
    make_act()
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


def test_receipt_date_comes_from_the_platform(sample_data):
    """Дата получения — площадки, а не «сейчас».

    По статусу «Получен» Ozon отдаёт весь архив: возвраты, полученные месяцы
    назад, приходят в том же ответе. Со временем «сейчас» первое же обновление
    объявило бы их полученными сегодня — так и вышел акт на 2446 позиций.
    """
    from app import ozon

    account = accounts.default_account()
    client = ozon.get_client(account)
    # Архив: возврат выдан продавцу давно, площадка помнит этот момент.
    old = client._returns[0]
    old["visual"]["status"]["sys_name"] = "ReceivedBySeller"
    old["visual"]["status"]["display_name"] = "Получен продавцом"
    old["logistic"]["final_moment"] = "2026-03-02T10:00:00+00:00"
    old["visual"]["change_moment"] = "2026-03-02T10:00:00+00:00"
    sync.sync_returns(account)

    row = db.query_one("SELECT received_day FROM returns WHERE id = ?", (str(old["id"]),))
    assert row["received_day"] == "2026-03-02", "архив записан сегодняшним числом"
    assert str(old["id"]) not in return_acts.received_returns(account["id"], store.local_day())


def test_receipt_date_comes_from_the_handover_not_the_status_change(sample_data):
    """Число берётся из final_moment — «выдан продавцу», а не из смены статуса.

    Выдали продавцу под полночь, а статус площадка перещёлкнула уже после неё.
    По смене статуса возврат уезжал в следующие сутки: шесть возвратов поездки
    вставали на одно число, седьмой — на другое, и в акт он не попадал.
    """
    from app import ozon

    account = accounts.default_account()
    client = ozon.get_client(account)
    ids = client.receive(
        final_moment="2026-05-10T18:50:00+00:00",   # выдали продавцу
        change_moment="2026-05-11T02:10:00+00:00",  # статус сменился позже
    )
    assert ids, "подделка не отдала ни одного возврата"
    sync.sync_returns(account)

    days = {
        row["received_day"] for row in db.query(
            f"SELECT received_day FROM returns WHERE id IN ({','.join('?' for _ in ids)})", ids
        )
    }
    assert days == {store.local_day("2026-05-10T18:50:00+00:00")}, (
        "возвраты одной поездки разъехались по числам"
    )
    assert len(return_acts.received_returns(
        account["id"], store.local_day("2026-05-10T18:50:00+00:00")
    )) == len(ids), "в акт попали не все возвраты поездки"


def test_unreadable_moment_does_not_lose_the_return(sample_data):
    """Момент не разобрать — число всё равно настоящее, а не строка от Ozon.

    Иначе в received_day оседает сама строка, такого числа нет ни в одном
    календаре, и возврат не найти: из «К выдаче» он ушёл, в акт не попадает.
    """
    from app import ozon

    account = accounts.default_account()
    client = ozon.get_client(account)
    ids = client.receive(final_moment="10.05.2026 18:50", change_moment="позавчера")
    assert ids
    sync.sync_returns(account)

    rows = db.query(
        f"SELECT received_day FROM returns WHERE id IN ({','.join('?' for _ in ids)})", ids
    )
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["received_day"]) for row in rows)
    # И находятся: за сегодня, раз у площадки внятного числа не нашлось.
    assert len(return_acts.received_returns(account["id"], store.local_day())) == len(ids)


def test_free_days_show_where_the_returns_wait(sample_data):
    """Возврат за другое число должен быть виден — иначе его не найти."""
    from app import ozon

    account = accounts.default_account()
    client = ozon.get_client(account)
    ready = [r for r in client._returns if r["visual"]["status"]["sys_name"] == "ArrivedAtReturnPlace"]
    assert len(ready) > 1, "для проверки нужно хотя бы два возврата в ПВЗ"

    client.receive(str(ready[0]["id"]), final_moment="2026-05-10T09:00:00+00:00")
    client.receive(*[str(r["id"]) for r in ready[1:]], final_moment="2026-05-12T09:00:00+00:00")
    sync.sync_returns(account)

    days = {item["day"]: item["count"] for item in return_acts.free_days(account["id"])}
    assert days.get(store.local_day("2026-05-10T09:00:00+00:00")) == 1
    assert days.get(store.local_day("2026-05-12T09:00:00+00:00")) == len(ready) - 1

    # Составили акт за одно число — оно уходит из списка, второе остаётся.
    make_act(day=store.local_day("2026-05-12T09:00:00+00:00"))
    left = {item["day"] for item in return_acts.free_days(account["id"])}
    assert store.local_day("2026-05-12T09:00:00+00:00") not in left
    assert store.local_day("2026-05-10T09:00:00+00:00") in left


def test_archive_does_not_get_into_todays_act(sample_data):
    """Акт за сегодня — только сегодняшняя поездка, без истории склада."""
    from app import ozon

    account = accounts.default_account()
    client = ozon.get_client(account)
    for item in client._returns[:5]:
        item["visual"]["status"]["sys_name"] = "ReceivedBySeller"
        item["visual"]["status"]["display_name"] = "Получен продавцом"
        item["logistic"]["final_moment"] = "2026-03-02T10:00:00+00:00"
        item["visual"]["change_moment"] = "2026-03-02T10:00:00+00:00"
    sync.sync_returns(account)

    today = [r["id"] for r in db.query("SELECT id FROM returns WHERE is_ready = 1")]
    client.receive(*today)
    sync.sync_returns(account)

    result = make_act(account)
    in_act = {row["id"] for row in return_acts.detail(result["act_id"])["ozon"]}
    assert in_act == set(today), "в сегодняшний акт попал архив площадки"


# ------------------------------------ несколько актов за одно число, без задвоения
def test_several_acts_a_day_split_the_returns(sample_data):
    """Две поездки за день — два акта, и в каждом только свои возвраты."""
    from app import ozon

    account = accounts.default_account()
    client = ozon.get_client(account)
    ready = [row["id"] for row in db.query("SELECT id FROM returns WHERE is_ready = 1")]
    assert len(ready) >= 3, "для проверки нужно хотя бы три возврата в ПВЗ"

    first_trip, second_trip = ready[:2], ready[2:]
    client.receive(*first_trip)
    sync.sync_returns(account)
    first = make_act()

    client.receive(*second_trip)
    sync.sync_returns(account)
    second = make_act()

    assert first["act_id"] != second["act_id"], "вторая поездка попала в акт первой"
    # Два акта одного числа различимы даже составленные в одну минуту.
    titles = [return_acts.detail(a)["title"] for a in (first["act_id"], second["act_id"])]
    assert titles[0] != titles[1], "акты одного числа неразличимы в списке"
    assert return_acts.get(second["act_id"])["day_seq"] == 2
    in_first = {row["id"] for row in return_acts.detail(first["act_id"])["ozon"]}
    in_second = {row["id"] for row in return_acts.detail(second["act_id"])["ozon"]}
    assert in_first & in_second == set(), "возврат оказался в двух актах сразу"
    assert set(second_trip) <= in_second
    assert set(second_trip) & in_first == set(), "второй завоз попал в первый акт"


def test_second_act_the_same_day_takes_only_the_new_returns(sample_data):
    """Возврат из первого акта во второй не переезжает и не дублируется."""
    from app import ozon

    account = accounts.default_account()
    client = ozon.get_client(account)
    ready = [row["id"] for row in db.query("SELECT id FROM returns WHERE is_ready = 1")]

    client.receive(ready[0])
    sync.sync_returns(account)
    first = make_act()
    in_first = {row["id"] for row in return_acts.detail(first["act_id"])["ozon"]}
    assert ready[0] in in_first

    client.receive(ready[1])
    sync.sync_returns(account)
    second = make_act()

    assert second["added"] == 1, "во второй акт попало не только новое"
    assert [row["id"] for row in return_acts.detail(second["act_id"])["ozon"]] == [ready[1]]
    assert {row["id"] for row in return_acts.detail(first["act_id"])["ozon"]} == in_first, \
        "первый акт изменился после составления второго"


def test_pressing_twice_with_nothing_new_makes_no_act(sample_data):
    """Нажали кнопку дважды подряд — второго акта нет и быть не должно."""
    take_everything()
    first = make_act()
    again = make_act()

    assert first["status"] == "ok" and first["act_id"]
    assert again["status"] == "warning" and again["act_id"] is None and again["added"] == 0
    assert len(return_acts.pending()) == 1, "пустой второй акт всё-таки завёлся"


def test_a_return_is_never_in_two_acts(sample_data):
    """Общее правило раздела, при любом порядке действий."""
    from app import ozon

    account = accounts.default_account()
    client = ozon.get_client(account)
    ready = [row["id"] for row in db.query("SELECT id FROM returns WHERE is_ready = 1")]
    for chunk in (ready[:1], ready[1:3], ready[3:]):
        client.receive(*chunk)
        sync.sync_returns(account)
        make_act()
        make_act()  # повтор вхолостую — акта из ничего быть не должно

    rows = db.query("SELECT id, act_id FROM returns WHERE act_id IS NOT NULL")
    assert len(rows) == len({row["id"] for row in rows}), "возврат числится в двух актах"
    total_in_acts = sum(act["total"] for act in return_acts.pending())
    assert total_in_acts == len(rows), "сумма по актам разошлась со строками"


# --------------------------------------------- защита от повторной загрузки
def test_repeat_sync_does_not_move_returns(acts):
    before = act_returns()
    sync.sync_returns(accounts.default_account())
    assert act_returns() == before, "повторная синхронизация перетасовала акты"


def test_return_still_reported_as_received_is_not_taken_twice(acts):
    """Ozon отдаёт «Получен» и дальше — в новый акт возврат не попадёт."""
    act = return_acts.pending()[0]
    before = {row["id"] for row in act["ozon"]}
    for _ in range(3):
        sync.sync_returns(accounts.default_account())
        make_act()
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
    assert make_act()["act_id"] is None, "подтверждённые возвраты собрались заново"
    assert return_acts.pending() == []
    still = {row["act_id"] for row in db.query("SELECT act_id FROM returns WHERE act_id IS NOT NULL")}
    assert still == {act["id"]}


# ------------------------------------------------------- выбор числа и кнопка
def test_only_the_chosen_day_gets_into_the_act(sample_data):
    """Акт за число — только это число: смешать дни значило бы смешать поездки."""
    account = accounts.default_account()
    take_everything()
    # Часть возвратов получена вчера: их акт сегодняшнего числа брать не должен.
    yesterday = store.local_time(
        (datetime.fromisoformat(db.now_iso()) - timedelta(days=1)).isoformat(), "%Y-%m-%d"
    )
    old = [row["id"] for row in db.query(
        "SELECT id FROM returns WHERE received_at IS NOT NULL LIMIT 2")]
    placeholders = ",".join("?" for _ in old)
    db.execute(f"UPDATE returns SET received_day = ? WHERE id IN ({placeholders})",
               [yesterday] + old)

    today = make_act(account, day=store.local_day())
    in_today = {row["id"] for row in return_acts.detail(today["act_id"])["ozon"]}
    assert in_today & set(old) == set(), "в акт попало чужое число"

    before = make_act(account, day=yesterday)
    assert sorted(row["id"] for row in return_acts.detail(before["act_id"])["ozon"]) == sorted(old)


def test_day_without_receipts_says_so(sample_data):
    account = accounts.default_account()
    result = return_acts.from_received(account["id"], "2001-01-01", user={"login": "admin"})
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


def test_packer_makes_an_act(client):
    """За возвратами ездит сборщик — он же составляет акт, без администратора."""
    csrf = login_as_packer(client)
    db.execute("DELETE FROM return_acts")
    db.execute("UPDATE returns SET act_id = NULL")

    response = client.post("/api/returns/acts/by-day", json={"day": store.local_day()},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    assert response.json()["act_id"]
    assert len(return_acts.pending()) == 1

    # Акт подписан тем, кто его завёл: по журналу видно, кто ездил.
    assert return_acts.pending()[0]["created_by"] == "packer9"


def test_returns_section_closed_means_no_act(client):
    """Раздел возвратов закрыт — закрыто и составление акта, не только кнопка."""
    csrf = login_as_packer(client)
    db.execute("UPDATE users SET sections = ? WHERE login = ?", ('["pack"]', "packer9"))
    response = client.post("/api/returns/acts/by-day", json={"day": store.local_day()},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 403


def test_day_endpoint_previews_before_creating(client):
    """Сначала видно, что попадёт в акт, и только потом акт составляется."""
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

    # Второе нажатие подряд: акта из ничего быть не должно.
    again = client.post("/api/returns/acts/by-day", json={"day": store.local_day()},
                        headers={"X-CSRF-Token": csrf})
    assert again.status_code == 200 and again.json()["act_id"] is None
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
    """Пришёл «Получен» — возврат переезжает из запасного акта в акт за число.

    Догадка о пропаже слабее факта получения, а два акта на один возврат — это
    две отметки на одну работу.
    """
    account = accounts.default_account()
    db.execute("DELETE FROM return_acts")
    db.execute("UPDATE returns SET act_id = NULL, received_at = NULL, received_day = NULL")

    target = db.query_one("SELECT id FROM returns WHERE is_ready = 1 LIMIT 1")["id"]
    spare = return_acts.collect_orphans(account["id"], [target])
    assert return_acts.detail(spare)["no_sheet"] is True

    take_everything(account)
    make_act(account)

    moved = db.query_one("SELECT act_id FROM returns WHERE id = ?", (target,))["act_id"]
    assert moved != spare, "возврат остался в запасном акте"
    assert return_acts.get(moved)["kind"] == return_acts.BY_DAY
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


# ---------------------------------------------------------------- шапка акта
def test_mark_answers_with_the_act_progress(client):
    """Отметив последний возврат, сборщик должен сразу увидеть, что акт готов.

    Кнопка «Подтвердить акт» живёт в шапке акта, а не в строке возврата.
    Поэтому ответ отметки несёт счётчики всего акта — по ним страница
    перерисовывает шапку, не перезагружаясь.
    """
    csrf = login(client)
    act = return_acts.pending()[0]
    rows = act["ozon"]
    assert len(rows) > 1, "для проверки нужен акт хотя бы из двух возвратов"

    first = client.post(
        "/api/returns/mark",
        json={"marketplace": "ozon", "id": rows[0]["id"], "mark": "ok", "note": ""},
        headers={"X-CSRF-Token": csrf},
    ).json()["act"]
    assert first["id"] == act["id"]
    assert first["marked_ok"] == 1 and first["unmarked"] == act["total"] - 1
    assert first["can_confirm"] is False

    for row in rows[1:-1]:
        mark(client, csrf, row["id"], "ok")
    last = client.post(
        "/api/returns/mark",
        json={"marketplace": "ozon", "id": rows[-1]["id"], "mark": "bad", "note": "вскрыт"},
        headers={"X-CSRF-Token": csrf},
    ).json()["act"]
    assert last["unmarked"] == 0 and last["marked_bad"] == 1
    assert last["percent"] == 100
    assert last["can_confirm"] is True, "кнопка «Подтвердить акт» так и не появится"


def test_mark_taken_back_closes_the_act_button_again(client):
    """Снятая отметка снова запирает подтверждение — тоже без перезагрузки."""
    csrf = login(client)
    act = return_acts.pending()[0]
    for row in act["ozon"]:
        mark(client, csrf, row["id"], "ok")

    body = client.post(
        "/api/returns/mark",
        json={"marketplace": "ozon", "id": act["ozon"][0]["id"], "mark": "", "note": ""},
        headers={"X-CSRF-Token": csrf},
    ).json()["act"]
    assert body["unmarked"] == 1 and body["can_confirm"] is False


def test_mark_outside_an_act_has_no_act_block(client):
    """Возврат из «К выдаче» ни в каком акте не состоит — перерисовывать нечего."""
    csrf = login(client)
    free = db.query_one("SELECT id FROM returns WHERE act_id IS NULL LIMIT 1")
    assert free, "в демо-данных не осталось возвратов вне актов"
    body = client.post(
        "/api/returns/mark",
        json={"marketplace": "ozon", "id": free["id"], "mark": "ok", "note": ""},
        headers={"X-CSRF-Token": csrf},
    ).json()
    assert body["act"] is None


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


# ------------------------------------------------------------------ переделать
def login_as_admin(client) -> str:
    """Администратор — не владелец: принятое переделывать ему не дают."""
    from app.security import hash_password

    db.execute(
        "INSERT INTO users(login, password_hash, role, active, created_at) VALUES(?,?,?,1,?)",
        ("admin9", hash_password("admin9123456"), "admin", db.now_iso()),
    )
    client.post("/login", data={"login": "admin9", "password": "admin9123456", "next": "/returns"})
    page = client.get("/returns")
    return re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1)


def confirmed_act(client, csrf) -> dict:
    """Акт, отмеченный и подтверждённый, — то, что лежит в «Отчётах»."""
    act = return_acts.pending()[0]
    for row in act["ozon"]:
        mark(client, csrf, row["id"], "ok", "цел")
    assert confirm(client, csrf, act["id"]).status_code == 200
    return act


def unconfirm(client, csrf, act_id):
    return client.post(f"/api/returns/acts/{act_id}/unconfirm", json={},
                       headers={"X-CSRF-Token": csrf})


def delete_act(client, csrf, act_id):
    return client.post(f"/api/returns/acts/{act_id}/delete", json={},
                       headers={"X-CSRF-Token": csrf})


def test_owner_returns_a_confirmed_act_to_work(client):
    """Подтвердили рано — акт возвращается в работу вместе с отметками."""
    csrf = login(client)
    act = confirmed_act(client, csrf)

    response = unconfirm(client, csrf, act["id"])
    assert response.status_code == 200, response.text
    assert any(a["id"] == act["id"] for a in return_acts.pending()), "акт не вернулся во вкладку"

    back = return_acts.detail(act["id"])
    assert back["confirmed_at"] is None and back["confirmed_by"] is None
    # Отметки на местах: править нужно строку, а не всю поездку.
    assert back["unmarked"] == 0 and back["can_confirm"] is True
    assert all(row["note"] == "цел" for row in back["ozon"])

    # И подтвердить его можно снова.
    assert confirm(client, csrf, act["id"]).status_code == 200


def test_unconfirming_an_open_act_says_so(client):
    csrf = login(client)
    act = return_acts.pending()[0]
    response = unconfirm(client, csrf, act["id"])
    assert response.status_code == 200
    assert response.json()["status"] == "warning"
    assert "не подтверждён" in response.json()["message"]


def test_owner_deletes_an_act_and_the_day_can_be_taken_again(client):
    """Удалённый акт освобождает возвраты — поездку принимают с нуля."""
    csrf = login(client)
    act = confirmed_act(client, csrf)
    ids = [row["id"] for row in act["ozon"]]
    account = accounts.default_account()

    response = delete_act(client, csrf, act["id"])
    assert response.status_code == 200, response.text
    assert response.json()["freed"] == act["total"]
    assert return_acts.get(act["id"]) is None
    assert not any(a["id"] == act["id"] for a in return_acts.pending())

    # Возвраты свободны и без отметок: иначе новый акт подтвердился бы сразу.
    for row in db.query(f"SELECT act_id, mark, note, mark_by FROM returns "
                        f"WHERE id IN ({','.join('?' for _ in ids)})", ids):
        assert row["act_id"] is None and row["mark"] is None
        assert row["note"] is None and row["mark_by"] is None

    # Момент получения — факт площадки, его не трогали: акт собирается за то же число.
    assert set(return_acts.received_returns(account["id"], store.local_day())) == set(ids)
    again = make_act()
    assert again["status"] == "ok" and again["added"] == len(ids)
    assert return_acts.detail(again["act_id"])["unmarked"] == len(ids)


def test_unconfirmed_act_can_be_deleted_too(client):
    """Собрали акт не за то число — его тоже надо уметь убрать."""
    csrf = login(client)
    act = return_acts.pending()[0]
    assert delete_act(client, csrf, act["id"]).status_code == 200
    assert return_acts.pending() == []


def test_deleting_says_when_to_make_the_act_again(client):
    csrf = login(client)
    act = return_acts.pending()[0]
    message = delete_act(client, csrf, act["id"]).json()["message"]
    assert store.local_time(db.now_iso(), "%d.%m.%Y") in message
    assert "заново" in message


def test_remaking_is_written_to_the_log(client):
    csrf = login(client)
    act = confirmed_act(client, csrf)
    unconfirm(client, csrf, act["id"])
    delete_act(client, csrf, act["id"])

    kinds = {row["kind"] for row in db.query("SELECT kind FROM events")}
    assert {"return_act_unconfirm", "return_act_delete"} <= kinds


def test_admin_cannot_remake_an_act(client):
    """Подпись под принятой работой снимает только тот, кто за склад отвечает."""
    owner_csrf = login(client)
    act = confirmed_act(client, owner_csrf)
    client.get("/logout")

    csrf = login_as_admin(client)
    # Администратор вошёл и работает — 403 именно про владельца, а не про вход.
    assert client.get("/returns?tab=acts").status_code == 200
    assert unconfirm(client, csrf, act["id"]).status_code == 403
    assert delete_act(client, csrf, act["id"]).status_code == 403
    assert return_acts.get(act["id"])["confirmed_at"], "администратор снял подтверждение"


def test_packer_cannot_remake_an_act(client):
    owner_csrf = login(client)
    act = confirmed_act(client, owner_csrf)
    client.get("/logout")

    csrf = login_as_packer(client)
    assert unconfirm(client, csrf, act["id"]).status_code == 403
    assert delete_act(client, csrf, act["id"]).status_code == 403


def test_remaking_requires_csrf(client):
    login(client)
    act = return_acts.pending()[0]
    assert client.post(f"/api/returns/acts/{act['id']}/unconfirm", json={}).status_code == 403
    assert client.post(f"/api/returns/acts/{act['id']}/delete", json={}).status_code == 403
    assert return_acts.get(act["id"]) is not None


def test_remaking_an_unknown_act_is_404(client):
    csrf = login(client)
    assert unconfirm(client, csrf, "нет-такого").status_code == 404
    assert delete_act(client, csrf, "нет-такого").status_code == 404


def test_buttons_are_owner_only(client):
    """Кнопок у администратора нет — не только запрет на сервере."""
    owner_csrf = login(client)
    act = confirmed_act(client, owner_csrf)
    page = client.get(f"/reports/returns/{act['id']}")
    assert "data-unconfirm-act" in page.text and "data-delete-act" in page.text
    client.get("/logout")

    login_as_admin(client)
    page = client.get(f"/reports/returns/{act['id']}")
    assert page.status_code == 200
    assert "data-unconfirm-act" not in page.text
    assert "data-delete-act" not in page.text


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


def test_acts_are_collapsed(client):
    """Акты свёрнуты: их за день несколько, и список на сотню строк съедает экран.

    Итоги и «Подтвердить акт» живут в заголовке — он виден и у свёрнутого акта,
    поэтому раскрывать заранее нечего.
    """
    login(client)
    page = client.get("/returns?tab=acts")
    for match in re.findall(r"<details class=\"act\"[^>]*>", page.text):
        assert " open" not in match, f"акт раскрыт по умолчанию: {match}"


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


# ------------------------------------------------- подсказки вместо стены текста
def test_explanations_live_under_the_hint(client):
    """Объяснение нужно один раз, а висит оно над рабочими кнопками всегда.

    Текст не выброшен — он под знаком «?»: спрятать совсем значило бы оставить
    новичка без ответа на «какие возвраты сюда попадают».
    """
    login(client)
    for url, marker in (("/returns", "Загружаются возвраты в статусе"),
                        ("/returns?tab=acts", "задвоить возврат нельзя")):
        page = client.get(url).text
        assert 'class="hint-body"' in page, f"{url}: подсказки нет"
        assert marker in page, f"{url}: текст подсказки потерялся"
        # Текст лежит внутри подсказки, а не отдельным абзацем над кнопками.
        body = page.split('class="hint-body"', 1)[1]
        assert marker in body.split("</span>", 1)[0] or marker in body[:2000]


def test_status_link_stays_reachable(client):
    """Внутри подсказки ссылка — иначе менять статусы стало бы негде."""
    login(client)
    page = client.get("/returns").text
    assert "Изменить статусы" in page and '/settings' in page
