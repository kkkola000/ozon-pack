"""Отметка о возврате и лист возвратов файлом PDF.

Возврат в пункте выдачи могут не отдать, отдать вскрытым или без комплекта.
Площадка про это не знает: у Ozon и Avito в статусах есть только «лежит в ПВЗ»
и «уехал дальше». Поэтому отметку и комментарий заводит панель — и они обязаны
пережить синхронизацию, иначе запись пропадёт ровно тогда, когда понадобится.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app.core import accounts, db, returns_pdf, store
from app.markets.avito import client as avito
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


def a_return() -> str:
    row = db.query_one(
        "SELECT id FROM returns WHERE account_id = ? AND is_ready = 1 ORDER BY id LIMIT 1",
        (accounts.default_account()["id"],),
    )
    assert row is not None, "в демо-данных нет возвратов, готовых к выдаче"
    return row["id"]


def mark(client, csrf, **payload):
    return client.post("/api/returns/mark", json=payload, headers={"X-CSRF-Token": csrf})


def stored(return_id: str) -> dict:
    row = db.query_one(
        "SELECT mark, note, mark_at, mark_by FROM returns WHERE account_id = ? AND id = ?",
        (accounts.default_account()["id"], return_id),
    )
    return dict(row)


# ---------------------------------------------------------------- сама отметка
def test_mark_and_comment_are_saved(client):
    csrf = login(client)
    return_id = a_return()

    response = mark(client, csrf, marketplace="ozon", id=return_id, mark="bad",
                    note="Коробка вскрыта, нет зарядки")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mark_label"] == "Не принят"
    assert body["mark_sign"] == "✗"

    row = stored(return_id)
    assert row["mark"] == "bad"
    assert row["note"] == "Коробка вскрыта, нет зарядки"
    assert row["mark_by"] == "admin"
    assert row["mark_at"], "не записано, когда поставили отметку"


def an_act_return() -> str:
    """Съездить за возвратами и свести их в акт — там и живут отметки."""
    from app.markets.ozon import client as ozon
    from app.core import return_acts, store, sync

    account = accounts.default_account()
    ozon.get_client(account).receive()
    sync.sync_returns(account)
    result = return_acts.from_received(account["id"], store.local_day(), user={"login": "admin"})
    assert result["status"] == "ok", result["message"]
    return return_acts.detail(result["act_id"])["ozon"][0]["id"]


def test_mark_shows_up_on_the_page(client):
    csrf = login(client)
    return_id = an_act_return()
    mark(client, csrf, marketplace="ozon", id=return_id, mark="ok", note="Всё на месте")

    page = client.get("/returns?tab=acts")
    assert page.status_code == 200
    assert "Принят" in page.text
    assert "Всё на месте" in page.text


def test_response_carries_everything_the_row_shows(client):
    """Ответ должен нести и автора со временем: строка обновляется без перезагрузки.

    Иначе рядом с новой отметкой останется висеть старый комментарий и старое
    имя — по списку будет видно то, чего уже нет.
    """
    csrf = login(client)
    body = mark(client, csrf, marketplace="ozon", id=a_return(), mark="ok", note="цел").json()
    assert body["note"] == "цел"
    assert body["mark_by"] == "admin"
    assert body["mark_at_local"], "не сказано, когда поставили отметку"

    cleared = mark(client, csrf, marketplace="ozon", id=a_return(), mark="", note="").json()
    assert cleared["note"] == "" and cleared["mark_by"] == "" and cleared["mark_at_local"] == ""


def test_mark_can_be_cleared(client):
    csrf = login(client)
    return_id = a_return()
    mark(client, csrf, marketplace="ozon", id=return_id, mark="ok", note="было")

    response = mark(client, csrf, marketplace="ozon", id=return_id, mark="", note="")
    assert response.status_code == 200
    row = stored(return_id)
    assert row["mark"] is None and row["note"] is None
    assert row["mark_at"] is None, "снятая отметка не должна оставлять время"


def test_sync_does_not_wipe_the_mark(client):
    """Главное свойство: обновление списка из Ozon отметку не трогает."""
    from app.core import sync

    csrf = login(client)
    return_id = a_return()
    mark(client, csrf, marketplace="ozon", id=return_id, mark="bad", note="Не отдали, спор")

    sync.sync_returns(accounts.default_account(), full=True)

    row = stored(return_id)
    assert row["mark"] == "bad", "синхронизация стёрла отметку"
    assert row["note"] == "Не отдали, спор", "синхронизация стёрла комментарий"


def test_unknown_mark_is_refused(client):
    csrf = login(client)
    response = mark(client, csrf, marketplace="ozon", id=a_return(), mark="maybe")
    assert response.status_code == 400
    assert stored(a_return())["mark"] is None


def test_unknown_return_is_refused(client):
    csrf = login(client)
    response = mark(client, csrf, marketplace="ozon", id="нет-такого", mark="ok")
    assert response.status_code == 404


def test_mark_from_another_cabinet_is_refused(client):
    """Возврат чужого кабинета отметить нельзя — кабинеты не смешиваются."""
    from app.core import sync

    csrf = login(client)
    second = accounts.get(accounts.create("ozon", "Второй Ozon", "test-client", "test-key"))
    sync.sync_returns(second)
    foreign = db.query_one(
        "SELECT id FROM returns WHERE account_id = ? AND id NOT IN "
        "(SELECT id FROM returns WHERE account_id = ?) LIMIT 1",
        (second["id"], accounts.default_account()["id"]),
    )
    if not foreign:
        pytest.skip("у второго кабинета нет собственных возвратов")
    assert mark(client, csrf, marketplace="ozon", id=foreign["id"], mark="ok").status_code == 404


def test_mark_requires_csrf(client):
    login(client)
    assert client.post("/api/returns/mark", json={"id": a_return(), "mark": "ok"}).status_code == 403


def test_mark_is_written_to_the_log(client):
    csrf = login(client)
    mark(client, csrf, marketplace="ozon", id=a_return(), mark="bad", note="брак")
    row = db.query_one("SELECT message FROM events WHERE kind = 'return_mark'")
    assert row is not None, "отметка не попала в журнал"
    assert "Не принят" in row["message"] and "брак" in row["message"]


# ------------------------------------------------------ где отметка есть, а где нет
def test_pickup_list_has_no_marks(client):
    """Возврат ещё лежит в ПВЗ — решать по нему нечего, его в руках не держали."""
    login(client)
    page = client.get("/returns")
    assert page.status_code == 200
    assert "data-mark-open" not in page.text, "на «К выдаче» осталась отметка"
    assert "Отметить" not in page.text
    # Список при этом на месте: убрали столбец, а не раздел.
    assert "Где лежит" in page.text


def test_mark_window_saves_by_the_decision_itself(client):
    """В окне отметки три кнопки-решения и крестик — «Сохранить» и «Отмены» нет.

    Сохранение было вторым нажатием по тому, что уже выбрали, и отметка
    терялась, когда окно закрывали крестиком, считая работу сделанной. Теперь
    записывает сама кнопка решения, а крестик ничего не пишет.
    """
    login(client)
    page = client.get("/returns?tab=acts")
    assert page.status_code == 200

    assert 'id="mark-close"' in page.text, "крестик в углу убрали"
    assert 'id="mark-clear"' in page.text
    assert 'data-mark="ok"' in page.text and 'data-mark="bad"' in page.text
    assert 'id="mark-save"' not in page.text, "кнопка «Сохранить» осталась"
    assert 'id="mark-cancel"' not in page.text, "кнопка «Отмена» осталась"
    assert ">Сохранить<" not in page.text and ">Отмена<" not in page.text

    # Комментарий выше кнопок: после нажатия писать уже некуда.
    assert page.text.index('id="mark-note"') < page.text.index('data-mark="bad"')


def test_marks_live_in_the_act(client):
    """Отмечают привезённое — во вкладке «Ждёт подтверждения», внутри акта."""
    from app.core import accounts, return_acts, store, sync
    from app.markets.ozon import client as ozon

    csrf = login(client)
    account = accounts.default_account()
    ozon.get_client(account).receive()
    sync.sync_returns(account)
    assert return_acts.from_received(
        account["id"], store.local_day(), user={"login": "admin"}
    )["status"] == "ok"

    page = client.get("/returns?tab=acts", headers={"X-CSRF-Token": csrf})
    assert page.text.count("data-mark-open") >= 1, "в акте нет чем отметить"


# ---------------------------------------------------------------- отметка у Avito
@pytest.fixture
def avito_cabinet(client):
    from app.core import sync

    cabinet = accounts.get(accounts.create("avito", "Кабинет Avito", "test-client", "test-secret"))
    sync.sync_avito(cabinet)
    return cabinet


def switch_to(client, csrf, account, where="/avito"):
    response = client.post(
        "/api/account/switch", json={"account_id": account["id"], "next": where},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text


def test_avito_return_can_be_marked(client, avito_cabinet):
    csrf = login(client)
    switch_to(client, csrf, avito_cabinet)
    order = db.query_one(
        "SELECT id FROM avito_orders WHERE account_id = ? AND status = ? LIMIT 1",
        (avito_cabinet["id"], avito.STATUS_ON_RETURN),
    )
    if not order:
        pytest.skip("в подделке Avito нет возвратов")

    response = mark(client, csrf, marketplace="avito", id=order["id"], mark="ok", note="принял целым")
    assert response.status_code == 200, response.text
    row = db.query_one(
        "SELECT mark, note FROM avito_orders WHERE account_id = ? AND id = ?",
        (avito_cabinet["id"], order["id"]),
    )
    assert row["mark"] == "ok" and row["note"] == "принял целым"

    page = client.get("/avito/returns")
    assert "принял целым" in page.text


def test_unknown_marketplace_is_refused(client):
    csrf = login(client)
    assert mark(client, csrf, marketplace="wildberries", id=a_return(), mark="ok").status_code == 400


# ---------------------------------------------------------------- отметка на листе
def test_printed_sheet_shows_marks(client):
    csrf = login(client)
    return_id = a_return()
    mark(client, csrf, marketplace="ozon", id=return_id, mark="bad", note="упаковка вскрыта")

    page = client.get("/returns/print")
    assert page.status_code == 200
    assert "Отметка" in page.text
    assert "упаковка вскрыта" in page.text
    assert "✗ Не принят" in page.text


# ---------------------------------------------------------------- лист файлом PDF
def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    from io import BytesIO

    return "\n".join(page.extract_text() for page in PdfReader(BytesIO(data)).pages)


def test_sheet_pdf_is_a_pdf_file(client):
    login(client)
    response = client.get("/returns/sheet.pdf")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.content[:5] == b"%PDF-"
    assert "attachment" in response.headers["content-disposition"]
    assert ".pdf" in response.headers["content-disposition"]


def test_sheet_pdf_holds_the_returns(client):
    login(client)
    ids = [
        row["id"] for row in db.query(
            "SELECT id FROM returns WHERE account_id = ? AND is_ready = 1",
            (accounts.default_account()["id"],),
        )
    ]
    assert ids, "в демо-данных нет возвратов, готовых к выдаче"

    text = pdf_text(client.get("/returns/sheet.pdf").content)
    for return_id in ids:
        assert str(return_id) in text, f"возврата {return_id} нет в PDF"


def test_sheet_pdf_holds_mark_and_comment(client):
    csrf = login(client)
    return_id = a_return()
    mark(client, csrf, marketplace="ozon", id=return_id, mark="bad", note="нет комплекта")

    text = pdf_text(client.get("/returns/sheet.pdf").content)
    assert "Не принят" in text
    assert "нет комплекта" in text


def test_sheet_pdf_prints_the_barcode_digits(client):
    """Рядом со штрихкодом нужен его номер: не считался сканером — наберут руками."""
    login(client)
    codes = [
        row["barcode"] for row in db.query(
            "SELECT barcode FROM returns WHERE account_id = ? AND is_ready = 1 AND barcode IS NOT NULL",
            (accounts.default_account()["id"],),
        )
    ]
    if not codes:
        pytest.skip("в демо-данных нет возвратов со штрихкодом")

    text = pdf_text(client.get("/returns/sheet.pdf").content)
    for code in codes:
        assert code in text, f"номера штрихкода {code} нет в PDF"


def test_sheet_pdf_keeps_the_scheme_visible(client):
    """Схема FBO/FBS нужна на листе: у них разные пункты выдачи."""
    login(client)
    text = pdf_text(client.get("/returns/sheet.pdf").content)
    schemes = {
        row["type"] or row["scheme"] for row in db.query(
            "SELECT type, scheme FROM returns WHERE account_id = ? AND is_ready = 1",
            (accounts.default_account()["id"],),
        )
    }
    for value in schemes - {None, ""}:
        assert value in text, f"схемы {value} нет в PDF"


def test_sheet_pdf_covers_every_cabinet(client):
    """scope=all — тот же охват, что и у листа для печати: Ozon и Avito вместе."""
    from app.core import sync

    login(client)
    second = accounts.get(accounts.create("ozon", "Второй Ozon", "test-client", "test-key"))
    sync.sync_returns(second)
    avito_cabinet = accounts.get(accounts.create("avito", "Кабинет Avito", "test-client", "test-secret"))
    sync.sync_avito(avito_cabinet)

    text = pdf_text(client.get("/returns/sheet.pdf?scope=all").content)
    assert "все кабинеты" in text
    assert "Второй Ozon" in text and "Кабинет Avito" in text
    assert "Ozon" in text and "Avito" in text


def test_sheet_pdf_respects_the_filter(client):
    """Фильтр кабинета работает так же, как на странице и на листе печати."""
    login(client)
    text = pdf_text(client.get("/returns/sheet.pdf?q=не-найдётся-такого").content)
    assert "Ни одного возврата" in text


def test_sheet_pdf_marks_rows_printed(client):
    login(client)
    assert client.get("/returns/sheet.pdf").status_code == 200
    left = db.query_one(
        "SELECT COUNT(*) AS c FROM returns WHERE account_id = ? AND is_ready = 1 AND printed_at IS NULL",
        (accounts.default_account()["id"],),
    )["c"]
    assert left == 0, "скачанный лист не отметил возвраты напечатанными"


def test_sheet_pdf_is_written_to_the_log(client):
    login(client)
    client.get("/returns/sheet.pdf")
    assert db.query_one("SELECT COUNT(*) AS c FROM events WHERE kind = 'returns_pdf'")["c"] == 1


def test_sheet_pdf_says_what_to_install_when_there_is_no_font(client, monkeypatch):
    """Без шрифта лист не собрать — надо сказать, что ставить, а не падать в 500."""
    monkeypatch.setattr(returns_pdf, "FONT_DIRS", ())
    monkeypatch.setenv("RETURNS_PDF_FONT", "")
    login(client)
    response = client.get("/returns/sheet.pdf")
    assert response.status_code == 503
    assert "fonts-dejavu-core" in response.json()["detail"]


def test_panel_starts_without_the_pdf_library():
    """Библиотека для одной кнопки не должна ронять панель целиком.

    Так уже случилось на сервере: обновились без pip install, и fpdf2, нужный
    только кнопке «Скачать PDF», не дал подняться ни входу, ни сканированию.
    Импорт fpdf2 теперь внутри сборки листа, и это надо удержать.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(returns_pdf.__file__).read_text())
    top_level = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level += [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top_level.append(node.module.split(".")[0])
    assert "fpdf" not in top_level, "fpdf2 импортируется наверху модуля — без него панель не поднимется"


def test_missing_pdf_library_says_what_to_install(client, monkeypatch):
    """Нет библиотеки — понятное сообщение и рабочая панель, а не отказ входа."""
    monkeypatch.setattr(returns_pdf, "_SHEET_CLASS", None)
    monkeypatch.setitem(__import__("sys").modules, "fpdf", None)
    login(client)
    response = client.get("/returns/sheet.pdf")
    assert response.status_code == 503
    assert "fpdf2" in response.json()["detail"]
    # Остальная панель работает
    assert client.get("/returns").status_code == 200
    assert client.get("/pack").status_code == 200


def test_returns_page_offers_the_pdf(client):
    login(client)
    page = client.get("/returns")
    assert "/returns/sheet.pdf" in page.text
    assert "Скачать PDF" in page.text


# ---------------------------------------------------------------- штрихкод в PDF
def svg_dark_bars(svg: bytes) -> list[tuple[float, float]]:
    """Тёмные полосы картинки: [(начало, ширина)] в модулях."""
    from xml.etree import ElementTree

    root = ElementTree.fromstring(svg.decode())
    return [
        (float(rect.get("x")), float(rect.get("width")))
        for rect in root.iter("{http://www.w3.org/2000/svg}rect")
    ]


def test_pdf_barcode_matches_the_one_on_screen():
    """Штрихкод в PDF и в HTML-листе обязан кодировать одно и то же.

    Разойдутся — в пункте выдачи отсканируется только один из двух листов.
    """
    from tests.code128 import encode_code128b

    expected = encode_code128b("RET-000123")
    bars = svg_dark_bars(returns_pdf.barcode_svg("RET-000123", height=20, module=1.0))
    assert bars, "в картинке штрихкода нет ни одной полосы"

    # Картинка рисует только тёмные полосы: чередование начинается с тёмной,
    # значит её ширины — это элементы с чётными номерами.
    position = 0.0
    drawn = []
    for index, width in enumerate(expected):
        if index % 2 == 0:
            drawn.append((position, float(width)))
        position += width
    assert bars == drawn


def test_mark_vocabulary_is_shared(client):
    """Подписи отметки живут в одном месте — иначе экран и лист разойдутся."""
    assert store.mark_label("ok") == "Принят"
    assert store.mark_label("bad") == "Не принят"
    assert store.mark_label(None) == ""
