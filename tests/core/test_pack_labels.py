"""Наклейки на «Сборке» — общие на все кабинеты.

Сборка объединена: сборщик стоит у одного стола и берёт заказы Ozon, Avito и
Маркета подряд. Значит, и наклейки нужны сразу все. Скачал стикеры одного
магазина, начал смену — и посреди неё наткнулся на заказ, ярлык которого уже
не взять: площадка отдаёт наклейку, пока заказ ждёт отгрузки.

Поэтому замок и архив считаются по всем кабинетам сразу, а фильтр площадки
сужает и то, и другое: выбрали «Avito» — качаются её этикетки.
"""
import io
import re
import zipfile
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.core import accounts
from app.main import app
from app.markets.avito import pack as avito_pack
from app.markets.avito import sync as avito_sync
from app.markets.ozon import pack as ozon_pack
from app.markets.yandex import client as yandex_client
from app.markets.yandex import pack as yandex_pack
from app.markets.yandex import sync as yandex_sync


@pytest.fixture
def warehouse(sample_data):
    """Склад с тремя кабинетами: Ozon по умолчанию, Avito и Маркет."""
    avito = accounts.get(accounts.create("avito", "Кабинет Avito", "test-client", "test-secret"))
    avito_sync.sync_avito(avito)
    yandex = accounts.get(accounts.create("yandex", "Маркет", "9000001", "test-token"))
    yandex_sync.sync_yandex(yandex)
    return {"ozon": accounts.default_account(), "avito": avito, "yandex": yandex}


@pytest.fixture
def client(warehouse):
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/pack"})
        page = test_client.get("/pack")
        test_client.headers["X-CSRF-Token"] = re.search(
            r'name="csrf-token" content="([^"]*)"', page.text).group(1)
        yield test_client


def pending(warehouse) -> dict[str, list[str]]:
    return {
        "ozon": ozon_pack.pending_labels(warehouse["ozon"]["id"]),
        "avito": avito_pack.pending_labels(warehouse["avito"]["id"]),
        "yandex": yandex_pack.pending_labels(warehouse["yandex"]["id"]),
    }


def names_in(archive: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        return sorted(zf.namelist())


# ---------------------------------------------------------------------- замок
def test_the_gate_counts_every_cabinet(client, warehouse):
    """Замок держит, пока не выгружено всё: считаем по трём кабинетам сразу."""
    waiting = pending(warehouse)
    assert all(waiting.values()), "в фикстуре должен ждать выгрузки каждый кабинет"

    labels = client.get("/api/pack/state").json()["labels"]
    assert labels["locked"] is True
    assert labels["pending"] == sum(len(keys) for keys in waiting.values())


def test_the_gate_says_whose_labels_are_waiting(client, warehouse):
    """«9 заказов» не отвечает на вопрос «чьих» — разбивка по магазинам нужна."""
    labels = client.get("/api/pack/state").json()["labels"]
    shops = {shop["title"]: shop for shop in labels["shops"]}

    assert set(shops) == {"Ozon", "Кабинет Avito", "Маркет"}
    # У каждой площадки наклейка называется по-своему — так её и зовут.
    assert shops["Ozon"]["word"] == "стикеры"
    assert shops["Кабинет Avito"]["word"] == "этикетки"
    assert shops["Маркет"]["word"] == "ярлыки"
    assert shops["Маркет"]["count"] == len(pending(warehouse)["yandex"])


def test_a_cabinet_with_nothing_to_download_is_not_shown(client, warehouse):
    """Выгруженный кабинет из замка уходит: работы по нему нет — строки нет."""
    assert client.post(f"/api/pack/labels.zip?shop={warehouse['avito']['id']}").status_code == 200

    shops = client.get("/api/pack/state").json()["labels"]["shops"]
    assert {shop["title"] for shop in shops} == {"Ozon", "Маркет"}


# ---------------------------------------------------------------------- архив
def test_one_archive_holds_the_labels_of_every_cabinet(client, warehouse):
    """Одно нажатие — один архив на все магазины, папка на кабинет."""
    waiting = pending(warehouse)
    response = client.post("/api/pack/labels.zip")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"

    names = names_in(response.content)
    folders = {name.split("/")[0] for name in names}
    assert folders == {"Ozon", "Кабинет Avito", "Маркет"}
    assert len(names) == sum(len(keys) for keys in waiting.values())
    assert response.headers["X-Labels-Saved"] == str(len(names))


def test_the_archive_opens_the_lock_for_everyone(client, warehouse):
    """После выгрузки пустеет очередь каждого кабинета, а не только текущего."""
    assert client.post("/api/pack/labels.zip").status_code == 200

    assert pending(warehouse) == {"ozon": [], "avito": [], "yandex": []}
    assert client.get("/api/pack/state").json()["labels"]["locked"] is False


def test_nothing_left_to_download_is_refused(client, warehouse):
    client.post("/api/pack/labels.zip")
    again = client.post("/api/pack/labels.zip")
    assert again.status_code == 400
    assert "уже выгружены" in again.json()["detail"]


def test_the_archive_needs_csrf(client, warehouse):
    assert client.post("/api/pack/labels.zip", headers={"X-CSRF-Token": ""}).status_code == 403


# --------------------------------------------------------------------- фильтр
def test_the_filter_narrows_the_gate(client, warehouse):
    """Выбран кабинет «Маркет» — замок считает его заказы, а не все подряд."""
    labels = client.get(f"/api/pack/state?shop={warehouse['yandex']['id']}").json()["labels"]
    assert [shop["title"] for shop in labels["shops"]] == ["Маркет"]
    assert labels["pending"] == len(pending(warehouse)["yandex"])


def test_the_filter_narrows_the_archive(client, warehouse):
    """Под фильтром качаются наклейки выбранного кабинета — остальные ждут."""
    before = pending(warehouse)
    response = client.post(f"/api/pack/labels.zip?shop={warehouse['yandex']['id']}")
    assert response.status_code == 200, response.text

    # Одна площадка — папки не нужно: лишний клик там ничего не объясняет.
    assert names_in(response.content) == sorted(f"{key}.pdf" for key in before["yandex"])
    after = pending(warehouse)
    assert after["yandex"] == []
    assert after["ozon"] == before["ozon"] and after["avito"] == before["avito"]


# ------------------------------------------------------------------- отказы
def break_yandex(monkeypatch, cabinet) -> None:
    """Маркет перестаёт отдавать ярлыки — как при его очередной поломке."""
    def refuse(*_args, **_kwargs):
        raise RuntimeError("Маркет прилёг")

    monkeypatch.setattr(yandex_client.get_client(cabinet), "labels_pdf", refuse)


def test_a_refusing_marketplace_does_not_stop_the_others(client, warehouse, monkeypatch):
    """Маркет отказал — стикеры Ozon и этикетки Avito всё равно уезжают.

    Иначе одна сломанная площадка оставляла бы сборщика вообще без наклеек, а
    смена не начиналась бы вовсе.
    """
    break_yandex(monkeypatch, warehouse["yandex"])
    before = pending(warehouse)

    response = client.post("/api/pack/labels.zip")
    assert response.status_code == 200, response.text
    assert unquote(response.headers["X-Labels-Refused"]) == "Маркет"

    after = pending(warehouse)
    assert after["ozon"] == [] and after["avito"] == []
    assert after["yandex"] == before["yandex"], "невыгруженное должно остаться в очереди"
    # Замок не открылся: ярлыков Маркета так и нет.
    assert client.get("/api/pack/state").json()["labels"]["locked"] is True


def test_everyone_refusing_is_an_error(client, warehouse, monkeypatch):
    """Ни одной наклейки — это отказ, а не пустой архив в браузере."""
    break_yandex(monkeypatch, warehouse["yandex"])

    response = client.post(f"/api/pack/labels.zip?shop={warehouse['yandex']['id']}")
    assert response.status_code == 502
    assert "Маркет" in response.json()["detail"]


# ------------------------------------------------------------- скачано ≠ напечатано
def test_the_archive_marks_downloaded_not_printed(client, warehouse):
    """Архив ставит «скачано» — по нему открывается замок. «Печатался» он не ставит.

    Раньше у Ozon выгрузка засчитывалась и как печать, а у Avito и Маркета —
    нет. Теперь одинаково: печать отмечает только печать.
    """
    from app.core import db, labels

    response = client.post("/api/pack/labels.zip")
    assert response.status_code == 200, response.text
    for table in ("postings", "avito_orders", "yandex_orders"):
        printed = db.query_one(f"SELECT COUNT(*) AS c FROM {table} WHERE print_count > 0")["c"]
        assert printed == 0, f"{table}: выгрузка засчиталась как печать"
    assert all(labels.state(keys)["locked"] is False for keys in pending(warehouse).values())

    # Новый заказ без отметки «скачано» — замок снова закрыт.
    number = db.query_one("SELECT posting_number FROM postings WHERE label_saved_at IS NOT NULL LIMIT 1")[0]
    db.execute("UPDATE postings SET label_saved_at = NULL WHERE posting_number = ?", (number,))
    state = client.get("/api/pack/state").json()["labels"]
    assert state["locked"] is True and state["pending"] == 1


# ------------------------------------------------------------- бронь при сквозной сборке
def test_opening_an_ozon_posting_frees_a_held_avito_order(warehouse, user):
    """Держал заказ Avito, открыл отправление Ozon — бронь Avito снята.

    Раньше Ozon снимал бронь только в своей таблице: заказ Avito висел
    забронированным до истечения CLAIM_TTL_MINUTES.
    """
    from app.core import db, pack_state
    from app.markets.ozon import store as ozon_store

    avito = warehouse["avito"]
    held = db.query_one("SELECT id FROM avito_orders WHERE account_id = ? AND status = 'ready_to_ship' LIMIT 1",
                        (avito["id"],))["id"]
    with db.write() as conn:
        pack_state.save(conn, avito, user, held, [])
    db.execute("UPDATE avito_orders SET claim_user_id = ?, claim_login = ?, claim_at = ? WHERE id = ?",
               (user["id"], user["login"], db.now_iso(), held))

    number = db.query_one("SELECT posting_number FROM postings WHERE status = ? AND local_state = 'new' LIMIT 1",
                          (ozon_store.STATUS_AWAITING_DELIVER,))[0]
    ozon_pack.select_posting(warehouse["ozon"], user, number)
    assert db.query_one("SELECT claim_login FROM avito_orders WHERE id = ?", (held,))["claim_login"] is None
    assert db.query_one("SELECT account_id, posting_number FROM pack_state WHERE user_id = ?",
                        (user["id"],))["posting_number"] == number
