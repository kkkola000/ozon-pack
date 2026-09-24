"""«Сборка» — один раздел на все площадки: фильтр площадок и общий обработчик.

Рабочее место у каждой площадки открывается по своему адресу, но страница одна:
её собирает routes/pack.py, а площадка объявляет только слова, плитки очереди и
откуда взять состояние сборщика. Фильтр вверху не просто прячет строки: выбрали
площадку — панель переводит рабочее место на её кабинет, иначе список показывал
бы одно, а сканирование искало другое.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app.core import accounts, sync
from app.main import app
from app.markets import registry
from app.markets.avito import sync as avito_sync
from app.markets.yandex import sync as yandex_sync


@pytest.fixture
def cabinets(sample_data):
    """Три площадки сразу: по кабинету на каждую, у всех свои заказы."""
    sync.sync_all()
    avito_one = accounts.get(accounts.create("avito", "Магазин Avito", "test-client", "test-secret"))
    avito_sync.sync_avito(avito_one)
    yandex_one = accounts.get(accounts.create("yandex", "Маркет", "9000001", "test-token"))
    yandex_sync.sync_yandex(yandex_one)
    return {"ozon": accounts.default_account(), "avito": avito_one, "yandex": yandex_one}


@pytest.fixture
def client(cabinets):
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/pack"})
        page = test_client.get("/pack")
        test_client.headers["X-CSRF-Token"] = re.search(
            r'name="csrf-token" content="([^"]*)"', page.text
        ).group(1)
        yield test_client


def rows_of(page: str) -> list[str]:
    """Площадки строк списка «Все заказы»."""
    return re.findall(r'<tr data-work="\d" data-market="([^"]+)"', page)


# ------------------------------------------------------------------ один обработчик
def test_every_market_opens_the_same_page(client, cabinets):
    """Три адреса — одна страница. Отдельных обработчиков у площадок больше нет."""
    for code, where in (("ozon", "/pack"), ("avito", "/avito/pack"), ("yandex", "/yandex/pack")):
        switched = client.post(
            "/api/account/switch", json={"account_id": cabinets[code]["id"], "next": where})
        assert switched.status_code == 200, switched.text
        page = client.get(where)
        assert page.status_code == 200, page.text[:300]
        assert 'id="scan"' in page.text, where
        assert "Все заказы" in page.text, where


def test_market_declares_its_workspace(client):
    """Площадка отдаёт слова и два числа — остальное собирает общий маршрут."""
    for market in registry.all_markets():
        workspace = market.workspace
        assert workspace is not None, market.code
        assert workspace.url and workspace.tab
        assert callable(workspace.load_state) and callable(workspace.count_queue)


# ------------------------------------------------------------------ фильтр площадок
def test_filter_shows_every_market_with_a_cabinet(client):
    """Чипы: «Все заказы» и площадки, у которых есть кабинет."""
    page = client.get("/pack").text
    assert "Все заказы" in page
    for market in registry.all_markets():
        assert f'?market={market.code}' in page, market.code


def test_filter_leaves_only_its_market(client):
    """Стоит фильтр — в списке только заказы этой площадки."""
    everything = rows_of(client.get("/pack").text)
    assert len(set(everything)) > 1, "в списке одна площадка — фильтр не на чем проверить"

    only_ozon = rows_of(client.get("/pack?market=ozon").text)
    assert only_ozon and set(only_ozon) == {"ozon"}
    assert len(only_ozon) < len(everything)


def test_filter_switches_the_workspace_to_that_market(client, cabinets):
    """Выбрали площадку — собираются её заказы: панель переводит кабинет на неё.

    Иначе фильтр обманывал бы: список показывал бы Avito, а сканирование
    продолжало искать товар в кабинете Ozon.
    """
    assert client.get("/pack").status_code == 200        # начинаем в кабинете Ozon
    # Чип Avito — ссылка на этой же странице: адрес чужой площадки отказал бы
    # раньше, чем панель успела переключить кабинет.
    moved = client.get("/pack?market=avito")
    assert moved.status_code == 303, moved.text
    assert moved.headers["location"] == "/avito/pack?market=avito"

    page = client.get("/avito/pack?market=avito")
    assert page.status_code == 200, page.text[:300]
    assert cabinets["avito"]["title"] in page.text
    assert set(rows_of(page.text)) == {"avito"}


def test_all_orders_is_the_default(client):
    """Без фильтра — весь список: это и есть «объединённая сборка»."""
    page = client.get("/pack").text
    assert len(set(rows_of(page))) > 1
    assert 'class="chip active"' in page


def test_counts_on_chips_are_for_the_whole_list(client):
    """Число на чипе — сколько всего у площадки, а не сколько осталось после фильтра."""
    everything = rows_of(client.get("/pack").text)
    filtered = client.get("/pack?market=ozon").text
    for code in set(everything):
        count = len([row for row in everything if row == code])
        assert f'?market={code}">' in filtered or f"?market={code}" in filtered
        assert f'<span class="badge">{count}</span>' in filtered, code


def test_unknown_filter_is_not_a_crash(client):
    """Кривой адрес не ломает рабочее место — показываем всё."""
    page = client.get("/pack?market=нет-такой")
    assert page.status_code == 200
    assert len(set(rows_of(page.text))) > 1


# ------------------------------------------------------------------ что убрали
def test_no_hint_marks_left(client):
    """Подсказок под знаком «?» в панели больше нет."""
    for where in ("/pack", "/returns", "/settings"):
        page = client.get(where)
        assert page.status_code == 200, where
        assert "hint-wrap" not in page.text, where
        assert "hint-body" not in page.text, where


def test_own_cabinet_is_not_highlighted(client):
    """Строку своего кабинета больше не подсвечиваем и не подписываем."""
    page = client.get("/pack").text
    assert 'class="own"' not in page and "текущий" not in page
