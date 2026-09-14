"""Загрузка акта выдачи файлом — запасной ход администратора.

Обычно акт приходит сам, методами /v1/return/giveout/*. Но метод включён не у
всех продавцов: тогда акт берут в личном кабинете Ozon и загружают файлом.
Разбираем по содержимому, а не по структуре формата — иначе под каждый вид
документа пришлось бы подстраиваться отдельно.
"""
import csv
import io
import json
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import accounts, act_upload, db, return_acts
from app.main import app
from app.security import hash_password


@pytest.fixture
def client(sample_data):
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def login(client, user="admin", password="test-admin-pass") -> str:
    response = client.post("/login", data={"login": user, "password": password, "next": "/returns"})
    assert response.status_code == 303, response.text
    page = client.get("/returns")
    return re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1)


def free_returns(limit=3):
    """Возвраты кабинета, которые ещё не разнесены по актам."""
    rows = db.query(
        "SELECT id, barcode, product_name FROM returns WHERE account_id = ? "
        "AND act_id IS NULL AND barcode IS NOT NULL ORDER BY id LIMIT ?",
        (accounts.default_account()["id"], limit),
    )
    assert rows, "в подделке не осталось возвратов без акта"
    return [dict(row) for row in rows]


def upload(client, csrf, name, data, *, dry_run=False):
    return client.post(
        "/api/returns/acts/upload",
        files={"file": (name, data)},
        data={"dry_run": "true" if dry_run else "false"},
        headers={"X-CSRF-Token": csrf, "X-Requested-With": "fetch"},
    )


def csv_act(rows) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["Штрихкод", "Товар"])
    for row in rows:
        writer.writerow([row["barcode"], row["product_name"]])
    return buffer.getvalue().encode("utf-8")


def xlsx_act(rows) -> bytes:
    """Минимальная книга Excel: этого достаточно, чтобы проверить разбор."""
    cells = "".join(
        f'<row r="{index + 1}"><c r="A{index + 1}" t="inlineStr">'
        f'<is><t>{row["barcode"]}</t></is></c></row>'
        for index, row in enumerate(rows)
    )
    sheet = (
        '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/'
        f'spreadsheetml/2006/main"><sheetData>{cells}</sheetData></worksheet>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as book:
        book.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        book.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


def pdf_act(rows) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_font("dv", "", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    pdf.set_font("dv", "", 11)
    pdf.add_page()
    pdf.cell(0, 8, "Акт выдачи возвратов", new_x="LMARGIN", new_y="NEXT")
    for row in rows:
        pdf.cell(0, 7, f'{row["barcode"]}  {row["product_name"]}', new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


# ---------------------------------------------------------------- разбор форматов
def test_codes_are_pulled_from_csv(sample_data):
    rows = free_returns()
    found = act_upload.parse("akt.csv", csv_act(rows))
    for row in rows:
        assert row["barcode"] in found


def test_codes_are_pulled_from_xlsx(sample_data):
    rows = free_returns()
    found = act_upload.parse("akt.xlsx", xlsx_act(rows))
    for row in rows:
        assert row["barcode"] in found


def test_codes_are_pulled_from_json(sample_data):
    """Сырой ответ API тоже годится — его проще всего скопировать."""
    rows = free_returns()
    payload = json.dumps({"result": {"articles": [{"barcode": r["barcode"]} for r in rows]}})
    found = act_upload.parse("otvet.json", payload.encode())
    for row in rows:
        assert row["barcode"] in found


def test_codes_are_pulled_from_pdf(sample_data):
    rows = free_returns()
    found = act_upload.parse("akt.pdf", pdf_act(rows))
    for row in rows:
        assert row["barcode"] in found


def test_cp1251_file_is_read(sample_data):
    """Выгрузки из Excel часто приходят в CP1251, а не в UTF-8."""
    rows = free_returns()
    text = "Штрихкод;Товар\n" + "\n".join(f'{r["barcode"]};{r["product_name"]}' for r in rows)
    found = act_upload.parse("akt.csv", text.encode("cp1251"))
    for row in rows:
        assert row["barcode"] in found


def test_short_words_are_not_codes():
    """Слова из текста акта в поиск попадать не должны."""
    found = act_upload.codes("Акт от 14.09 ПВЗ шт да нет RET90000000")
    assert "RET90000000" in found
    assert "шт" not in found and "да" not in found


def test_empty_file_is_refused():
    with pytest.raises(act_upload.UploadRejected, match="пустой"):
        act_upload.parse("akt.csv", b"")


def test_huge_file_is_refused():
    with pytest.raises(act_upload.UploadRejected, match="больше"):
        act_upload.parse("akt.csv", b"x" * (act_upload.MAX_BYTES + 1))


def test_file_without_codes_says_so():
    with pytest.raises(act_upload.UploadRejected, match="ни одного кода"):
        act_upload.parse("akt.txt", "просто текст без кодов".encode())


def test_broken_pdf_is_refused():
    with pytest.raises(act_upload.UploadRejected, match="PDF"):
        act_upload.parse("akt.pdf", "%PDF-1.4 ломаный файл".encode())


def test_missing_pypdf_says_what_to_install(monkeypatch):
    """Библиотеки нет — понятное сообщение, а не отказ всей загрузки."""
    monkeypatch.setitem(__import__("sys").modules, "pypdf", None)
    with pytest.raises(act_upload.UploadRejected, match="pypdf"):
        act_upload.parse("akt.pdf", b"%PDF-1.4")


def test_panel_starts_without_pypdf():
    """Разбор PDF — одна кнопка, он не должен ронять панель."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("app/act_upload.py").read_text())
    top = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top += [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top.append(node.module.split(".")[0])
    assert "pypdf" not in top, "pypdf импортируется наверху модуля"


# ---------------------------------------------------------------- загрузка в панель
def test_preview_shows_what_was_found(client):
    """Сначала показываем, что нашлось: акт удалить нельзя."""
    csrf = login(client)
    rows = free_returns()
    before = {a["id"] for a in return_acts.pending()}

    response = upload(client, csrf, "akt.csv", csv_act(rows), dry_run=True)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["found"] == len(rows)
    assert {item["id"] for item in body["returns"]} == {row["id"] for row in rows}
    assert {a["id"] for a in return_acts.pending()} == before, "предпросмотр завёл акт"


def test_upload_creates_the_act(client):
    csrf = login(client)
    rows = free_returns()

    response = upload(client, csrf, "akt-14-09.csv", csv_act(rows))
    assert response.status_code == 200, response.text
    act_id = response.json()["act_id"]

    act = return_acts.detail(act_id)
    assert {r["id"] for r in act["ozon"]} == {row["id"] for row in rows}
    assert act["uploaded"] is True
    assert "akt-14-09.csv" in act["giveout_label"]
    assert act["created_by"] == "admin"


def test_uploaded_act_works_like_any_other(client):
    """По загруженному акту так же ставят отметки и подтверждают его."""
    csrf = login(client)
    rows = free_returns(2)
    act_id = upload(client, csrf, "akt.csv", csv_act(rows)).json()["act_id"]

    for row in rows:
        assert client.post(
            "/api/returns/mark",
            json={"marketplace": "ozon", "id": row["id"], "mark": "ok", "note": "цел"},
            headers={"X-CSRF-Token": csrf},
        ).status_code == 200

    response = client.post(f"/api/returns/acts/{act_id}/confirm", json={},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    assert return_acts.get(act_id)["confirmed_by"] == "admin"


def test_page_says_the_act_came_from_a_file(client):
    """В списке должно быть видно, что акт загружен, а не пришёл от площадки."""
    csrf = login(client)
    upload(client, csrf, "akt-14-09.csv", csv_act(free_returns()))

    page = client.get("/returns?tab=acts")
    assert "загружен файлом: akt-14-09.csv" in page.text
    assert "загрузил admin" in page.text
    assert "лист печатал" not in page.text


def test_upload_is_written_to_the_log(client):
    csrf = login(client)
    upload(client, csrf, "akt.csv", csv_act(free_returns()))
    row = db.query_one("SELECT message FROM events WHERE kind = 'return_act_upload'")
    assert row is not None, "загрузка акта не попала в журнал"
    assert "akt.csv" in row["message"]


def test_file_from_another_cabinet_creates_nothing(client):
    """Перепутали кабинет — панель скажет об этом, а не заведёт пустой акт."""
    from app import sync

    csrf = login(client)
    second = accounts.get(accounts.create("ozon", "Второй Ozon", "test-client", "test-key"))
    sync.sync_returns(second)
    foreign = [
        dict(row) for row in db.query(
            "SELECT barcode, product_name FROM returns WHERE account_id = ? AND barcode IS NOT NULL LIMIT 3",
            (second["id"],),
        )
    ]
    before = {a["id"] for a in return_acts.pending()}

    body = upload(client, csrf, "chuzhoy.csv", csv_act(foreign)).json()
    assert body["status"] == "warning"
    assert body["found"] == 0
    assert "ни один не совпал" in body["message"]
    assert {a["id"] for a in return_acts.pending()} == before


def test_returns_already_in_an_act_are_marked_in_the_preview(client):
    csrf = login(client)
    rows = free_returns(2)
    upload(client, csrf, "akt.csv", csv_act(rows))

    body = upload(client, csrf, "akt.csv", csv_act(rows), dry_run=True).json()
    assert body["busy"] == len(rows)
    assert all(item["in_act"] for item in body["returns"])


def test_second_upload_of_the_same_file_is_refused(client):
    """Дважды один акт — это два комплекта отметок на одну работу."""
    csrf = login(client)
    rows = free_returns(2)
    upload(client, csrf, "akt.csv", csv_act(rows))

    again = upload(client, csrf, "akt.csv", csv_act(rows))
    assert again.status_code == 409
    assert "уже разнесены" in again.json()["detail"]


# ---------------------------------------------------------------- кто может грузить
def test_upload_is_admin_only(client):
    db.execute(
        "INSERT INTO users(login, password_hash, role, active, created_at) "
        "VALUES('sklad', ?, 'packer', 1, ?)",
        (hash_password("secret123"), db.now_iso()),
    )
    csrf = login(client, "sklad", "secret123")
    response = upload(client, csrf, "akt.csv", csv_act(free_returns()))
    assert response.status_code == 403


def test_upload_requires_csrf(client):
    login(client)
    response = client.post(
        "/api/returns/acts/upload",
        files={"file": ("akt.csv", csv_act(free_returns()))},
        data={"dry_run": "false"},
    )
    assert response.status_code == 403


def test_packer_does_not_see_the_upload_block(client):
    db.execute(
        "INSERT INTO users(login, password_hash, role, active, created_at) "
        "VALUES('sklad2', ?, 'packer', 1, ?)",
        (hash_password("secret123"), db.now_iso()),
    )
    login(client, "sklad2", "secret123")
    page = client.get("/returns?tab=acts")
    assert page.status_code == 200
    assert "Загрузить акт файлом" not in page.text


def test_admin_sees_the_upload_block(client):
    login(client)
    page = client.get("/returns?tab=acts")
    assert "Загрузить акт файлом" in page.text
    assert "/api/returns/acts/upload" in client.get("/static/returns.js").text
