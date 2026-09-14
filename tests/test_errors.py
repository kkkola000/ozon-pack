"""Как панель сообщает об ошибках.

Сообщения русские, и показать их надо так, чтобы человек прочитал. Дважды
выходило иначе: ответ уходил без указания кодировки и на русской Windows
превращался в «РўСЂРµР±СѓРµС‚СЃСЏ РІС…РѕРґ», а по ссылке на выгрузку оператор
получал голый JSON вместо объяснения.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app

# Так заголовок Accept присылает браузер, когда человек переходит по ссылке.
AS_BROWSER = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,*/*;q=0.8"}
# А так — когда запрашивает скрипт панели.
AS_SCRIPT = {"Accept": "application/json", "X-Requested-With": "fetch"}


@pytest.fixture
def client(sample_data):
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def login(client) -> str:
    response = client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/returns"})
    assert response.status_code == 303, response.text
    page = client.get("/returns")
    return re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1)


# ---------------------------------------------------------------- кодировка
def test_json_errors_declare_utf8(client):
    """Без charset браузер угадывает кодировку по системе — и угадывает CP1251."""
    response = client.post("/api/scan", json={"code": "1"}, headers=AS_SCRIPT)
    assert response.status_code == 401
    assert "charset=utf-8" in response.headers["content-type"].lower()
    assert response.json()["detail"] == "Требуется вход"


def test_russian_message_survives_the_trip(client):
    """Проверка «в лоб»: байты ответа читаются как UTF-8 и совпадают с текстом."""
    response = client.post("/api/scan", json={"code": "1"}, headers=AS_SCRIPT)
    assert "Требуется вход" in response.content.decode("utf-8")
    # Так это выглядело у оператора, когда кодировка не была указана
    assert "РўСЂРµР±СѓРµС‚СЃСЏ" not in response.text


def test_successful_json_declares_utf8_too(client):
    login(client)
    response = client.get("/api/state", headers=AS_SCRIPT)
    assert response.status_code == 200
    assert "charset=utf-8" in response.headers["content-type"].lower()


def test_api_errors_declare_utf8(client):
    csrf = login(client)
    response = client.post("/api/returns/mark",
                           json={"marketplace": "ozon", "id": "нет-такого", "mark": "ok"},
                           headers={**AS_SCRIPT, "X-CSRF-Token": csrf})
    assert response.status_code == 404
    assert "charset=utf-8" in response.headers["content-type"].lower()
    assert "не найден" in response.json()["detail"]


# ---------------------------------------------------------------- что видит человек
def test_link_shows_a_page_not_json(client):
    """По ссылке открывается объяснение, а не {"detail": ...}."""
    login(client)
    response = client.get("/returns/acts/нет-такого.pdf", headers=AS_BROWSER)
    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert "Акт не найден" in response.text
    assert '{"detail"' not in response.text
    # С такой страницы есть куда вернуться
    assert "К возвратам" in response.text


def test_script_still_gets_json(client):
    """Скрипту панели по-прежнему нужен JSON — его разбирает api()."""
    login(client)
    response = client.get("/returns/acts/нет-такого.pdf", headers=AS_SCRIPT)
    assert response.status_code == 404
    assert "application/json" in response.headers["content-type"]
    assert response.json()["detail"] == "Акт не найден"


def test_missing_pdf_library_is_explained_on_a_page(client, monkeypatch):
    """Ровно тот случай, что был у оператора: кнопка «Скачать PDF» без fpdf2."""
    from app import returns_pdf

    monkeypatch.setattr(returns_pdf, "_SHEET_CLASS", None)
    monkeypatch.setitem(__import__("sys").modules, "fpdf", None)
    login(client)

    response = client.get("/returns/sheet.pdf", headers=AS_BROWSER)
    assert response.status_code == 503
    assert "text/html" in response.headers["content-type"]
    assert "fpdf2" in response.text
    assert "Пока недоступно" in response.text


def test_error_page_keeps_the_status_code(client):
    login(client)
    response = client.get("/returns/acts/нет-такого.pdf", headers=AS_BROWSER)
    assert response.status_code == 404, "страница с объяснением не должна отвечать 200"
