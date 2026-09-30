"""«Принтеры»: наклейка от площадки, своя бумага и подгонка печати (⚙).

Три разные вещи — три настройки. Какую наклейку просить у площадки (у Ozon
маленький стикер или большой, у Маркета формат ярлыка) — выбор документа. На
какую бумагу печатать — строка: готовый размер или свой, в мм. Зазор между
этикетками и сдвиг печати — подгонка строки, только для QZ Tray: её делает
сервер прямо в PDF.
"""
import base64
import io
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

from app.core import printers
from app.main import app
from app.markets.ozon import client as ozon_client
from app.markets.yandex import client as yandex_client

STATIC = Path(__file__).resolve().parents[2] / "app" / "static"
MM = 72 / 25.4


@pytest.fixture
def client(sample_data):
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/pack"})
        token = re.search(r'name="csrf-token" content="([^"]*)"', test_client.get("/pack").text).group(1)
        test_client.headers["X-CSRF-Token"] = token
        yield test_client


def save(client, rows, formats=None):
    body = {"rows": rows}
    if formats is not None:
        body["formats"] = formats
    return client.post("/api/printers", json=body)


def ozon_row(**extra):
    return {"kind": "ozon:label", "size": "58x40", "printer": "Xprinter XP-365B", "orientation": "", **extra}


def blank_pdf(width_mm=58, height_mm=40, rotate=0) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=width_mm * MM, height=height_mm * MM)
    if rotate:
        page.rotate(rotate)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ------------------------------------------------------------------ своя бумага
def test_custom_paper_is_saved_and_named(client):
    answer = save(client, [ozon_row(size="custom", width="60", height="40,5")])
    assert answer.status_code == 200, answer.text
    row = next(r for r in printers.rows() if r["kind"] == "ozon:label")
    assert (row["size"], row["width"], row["height"]) == ("custom", 60.0, 40.5)
    assert printers.paper_title(row) == "60×40,5 мм"
    assert printers.paper_mm(row) == (60.0, 40.5)
    # Браузер получает то же — по нему QZ Tray задаёт бумагу.
    setup_row = next(r for r in printers.setup()["rows"] if r["kind"] == "ozon:label")
    assert setup_row["width"] == 60.0 and setup_row["height"] == 40.5


@pytest.mark.parametrize("rows, why", [
    ([ozon_row(size="custom", width="5", height="40")], "меньше 10 мм"),
    ([ozon_row(size="custom", width="60", height="много")], "не число"),
    ([ozon_row(size="custom", width="58", height="40"), ozon_row()], "та же бумага дважды"),
    ([ozon_row(gap="25")], "зазор больше 20 мм"),
    ([ozon_row(shift={"right": "-2"})], "отрицательный сдвиг"),
    ([ozon_row(shift={"left": "31"})], "сдвиг больше 30 мм"),
])
def test_nonsense_paper_and_fit_are_refused(client, rows, why):
    answer = save(client, rows)
    assert answer.status_code == 400, why
    assert all(not r.get("printer") for r in printers.rows()), why


# ------------------------------------------------------------------ подгонка строки
def test_gap_and_shift_are_saved_and_shown(client):
    answer = save(client, [ozon_row(gap="2", shift={"right": "2", "bottom": "1", "top": "0"})])
    assert answer.status_code == 200, answer.text
    row = next(r for r in printers.rows() if r["kind"] == "ozon:label")
    assert row["gap"] == 2.0 and row["shift"] == {"right": 2.0, "bottom": 1.0}
    assert printers.fit_title(row) == "зазор 2 · вправо 2 · вниз 1"
    page = client.get("/printers").text
    assert "зазор 2 · вправо 2 · вниз 1" in page
    assert re.search(r'class="btn small btn-fit set"[^>]*data-gap="2"[^>]*data-right="2"', page, re.S)
    assert 'id="fit-modal"' in page and 'data-side="top"' in page and 'data-side="left"' in page


def test_rows_without_fit_stay_as_they_were():
    """Строка без подгонки — прежние четыре поля: настройка из старых версий читается как была."""
    assert set(next(r for r in printers.rows() if r["kind"] == "ozon:label")) == {
        "kind", "size", "printer", "orientation"}


# ------------------------------------------------------------------ наклейка от площадки
def test_label_format_follows_the_paper_until_chosen(client):
    """Пока наклейку не выбрали — как раньше: по бумаге первой строки."""
    save(client, [ozon_row(), {"kind": "yandex:label", "size": "58x40", "printer": "", "orientation": ""}])
    assert printers.label_format("ozon:label") == "small"
    assert yandex_client.label_format() == "A9_HORIZONTALLY"
    assert printers.label_format("avito:label") is None, "размер Avito решает площадка"


def test_label_format_is_chosen_apart_from_the_paper(client):
    answer = save(client, [ozon_row(size="custom", width="60", height="40")],
                  formats={"ozon:label": "big", "yandex:label": "A4"})
    assert answer.status_code == 200, answer.text
    assert ozon_client.label_format() == "big", "бумага 60×40, а стикер — большой, как выбрали"
    assert yandex_client.label_format() == "A4"
    page = client.get("/printers").text
    assert re.search(r'<select class="format-select" data-kind="ozon:label">.*?'
                     r'<option value="big" selected>', page, re.S)
    assert "Стикер от Ozon" in page and "Ярлык от Маркета" in page
    assert 'data-kind="avito:label">' not in page.split('class="format-select"')[-1][:40]


@pytest.mark.parametrize("formats", [{"avito:label": "small"}, {"ozon:label": "huge"}, {"poster": "a4"}])
def test_unknown_label_format_is_refused(client, formats):
    assert save(client, [ozon_row()], formats=formats).status_code == 400


def test_ozon_takes_the_chosen_label_task(client, monkeypatch):
    """Ozon делает задание на обе этикетки — панель берёт выбранную, а не по бумаге."""
    real = ozon_client.OzonClient("test-client", "test-key", max_retries=1)
    tasks = {"result": {"tasks": [{"task_id": 1, "task_type": "small_label"},
                                  {"task_id": 2, "task_type": "big_label"}]}}
    monkeypatch.setattr(real, "post", lambda _path, _payload: tasks)
    save(client, [ozon_row()], formats={"ozon:label": "big"})
    assert real.label_task(["1-1-1"]) == 2, "бумага 58×40, но выбран большой стикер"
    save(client, [ozon_row(size="75x120")], formats={"ozon:label": "small"})
    assert real.label_task(["1-1-1"]) == 1


# ------------------------------------------------------------------ подгонка PDF
def test_fit_moves_the_content_and_grows_the_page_by_the_gap():
    fitted = printers.fit_pdf(printers.sample_pdf(58, 40, "Проба"), gap=2, right=2, bottom=1)
    page = PdfReader(io.BytesIO(fitted)).pages[0]
    assert round(float(page.mediabox.width) / MM, 1) == 58.0
    assert round(float(page.mediabox.height) / MM, 1) == 42.0, "длина листа — этикетка плюс зазор"
    content = page.get_contents().get_data().decode("latin-1")
    dx, dy = re.search(r"1 0(?:\.0)? 0(?:\.0)? 1 (-?[\d.]+) (-?[\d.]+) cm", content).groups()
    assert round(float(dx) / MM, 1) == 2.0 and round(float(dy) / MM, 1) == -1.0, "вправо 2, вниз 1"


def test_fit_straightens_a_rotated_page_first():
    fitted = printers.fit_pdf(blank_pdf(40, 58, rotate=90), right=1)
    page = PdfReader(io.BytesIO(fitted)).pages[0]
    assert int(page.get("/Rotate") or 0) % 360 == 0, "сдвиг «вправо» — вправо на бумаге, а не вниз"


def test_fit_api_returns_the_fitted_pdf(client):
    raw = base64.b64encode(blank_pdf()).decode()
    answer = client.post("/api/printers/fit", json={"pdf": raw, "gap": "2", "right": "1,5"})
    assert answer.status_code == 200, answer.text
    page = PdfReader(io.BytesIO(base64.b64decode(answer.json()["pdf"]))).pages[0]
    assert round(float(page.mediabox.height) / MM, 1) == 42.0


@pytest.mark.parametrize("body", [{"pdf": "не base64!"}, {"pdf": "", "gap": 1},
                                  {"pdf": base64.b64encode(b"x").decode(), "gap": "99"}])
def test_fit_api_refuses_nonsense(client, body):
    assert client.post("/api/printers/fit", json=body).status_code == 400


def test_fit_api_needs_csrf(client):
    token = client.headers.pop("X-CSRF-Token")
    answer = client.post("/api/printers/fit", json={"pdf": base64.b64encode(blank_pdf()).decode()})
    client.headers["X-CSRF-Token"] = token
    assert answer.status_code == 403


def test_sample_label_is_the_paper_size(client):
    answer = client.get("/api/printers/sample.pdf", params={"width": "60", "height": "40", "title": "Проба"})
    assert answer.status_code == 200 and answer.content.startswith(b"%PDF")
    assert answer.headers["x-page-size"] == "60x40"
    assert client.get("/api/printers/sample.pdf", params={"width": "0"}).status_code == 400


# ------------------------------------------------------------------ браузер
def test_qz_prints_the_row_paper_with_gap_and_fit():
    script = (STATIC / "qz.js").read_text(encoding="utf-8")
    assert "row.size === 'custom'" in script, "своя бумага строки"
    assert "height + (Number(row.gap) || 0)" in script, "зазор — к длине листа"
    assert "/api/printers/fit" in script and "/api/printers/sample.pdf" in script


def test_page_has_custom_paper_and_label_choice(client):
    page = client.get("/printers").text
    assert '<option value="custom"' in page and "Свой размер…" in page
    assert page.count('class="format-select"') == 2, "выбор наклейки — у Ozon и у Маркета"
    assert "Бумага для печати" in page
