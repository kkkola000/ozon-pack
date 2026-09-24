"""«Сборка» — один раздел на все площадки: фильтр площадок и общий обработчик.

Рабочее место открывается по адресу любой площадки, но страница одна: её
собирает routes/pack.py, а площадка объявляет только слова, плитки очереди,
как сканировать и чей это код.

Кабинет в шапке тут ничего не решает. Решает фильтр: «Все заказы» — сборка по
всем кабинетам, выбрана площадка — в её границах. Кабинет при этом не
переключается: страница про это больше не знает.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app.core import accounts, db, sync
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


def forget_sync():
    """Забыть, когда кабинеты ходили на площадку: иначе сработает защита от частых походов."""
    from app.core import sync as core_sync

    db.execute(
        "DELETE FROM kv WHERE key LIKE ? OR key LIKE ?",
        (f"{core_sync.KV_ACCOUNT_SYNC}:%", f"{core_sync.KV_ACCOUNT_TRIED}:%"),
    )


def cabinet_of(client) -> str | None:
    """Какой кабинет сейчас открыт в шапке — по куке переключателя."""
    from app.core.deps import ACCOUNT_COOKIE

    return client.cookies.get(ACCOUNT_COOKIE)


def rows_of(page: str) -> list[str]:
    """Площадки строк списка «Все заказы»."""
    return re.findall(r'<tr data-work="\d" data-market="([^"]+)"', page)


# ------------------------------------------------------------------ один обработчик
def test_a_market_url_opens_without_switching_cabinets(client, cabinets):
    """Адрес площадки — просто дверь: кабинет в шапке он не трогает.

    Раньше такая ссылка либо отказывала, либо молча переводила кабинет. Теперь
    ни того, ни другого: страница одна, а границы сборки задаёт фильтр.
    """
    client.post("/api/account/switch", json={"account_id": cabinets["ozon"]["id"], "next": "/pack"})
    before = cabinet_of(client)
    page = client.get("/yandex/pack")
    assert page.status_code == 200, page.text[:300]
    assert len(set(rows_of(page.text))) > 1, "фильтр по умолчанию — «Все заказы»"
    assert cabinet_of(client) == before, "кабинет всё-таки переключился"


def test_every_market_opens_the_same_page(client, cabinets):
    """Три адреса — одна страница, в любом кабинете. Дверей много, комната одна."""
    for where in ("/pack", "/avito/pack", "/yandex/pack"):
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
        # Скан площадка тоже объявляет: ядро не ходит в её ручки напрямую.
        assert callable(workspace.owner) and callable(workspace.scan)
        assert callable(workspace.release)


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


def test_filter_narrows_without_touching_the_cabinet(client, cabinets):
    """Выбрали площадку — список её, а кабинет в шапке остался прежним.

    Фильтр задаёт границы сборки напрямую, поэтому переключать кабинет ради
    него больше не нужно — и не надо: человек не просил менять шапку.
    """
    assert client.get("/pack").status_code == 200        # начинаем в кабинете Ozon
    before = cabinet_of(client)
    page = client.get("/pack?market=avito")
    assert page.status_code == 200, page.text[:300]
    assert set(rows_of(page.text)) == {"avito"}
    assert cabinet_of(client) == before, "кабинет всё-таки переключился"
    # И чип ведёт на текущий адрес, а не на адрес чужой площадки.
    assert '"/pack?market=avito"' in page.text


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

# ------------------------------------------------------------------ свежесть
def test_opening_the_page_asks_every_marketplace(client, cabinets, monkeypatch):
    """Открыли «Сборку» — панель сходила во все кабинеты под фильтром.

    Список общий, значит и свежесть общая: обновить один кабинет и показать
    рядом вчерашние заказы соседнего — это и есть «панель врёт».
    """
    from app.core import sync as core_sync

    calls = []
    monkeypatch.setattr(core_sync, "sync_account",
                        lambda account, **kw: calls.append(account["id"]) or {})
    forget_sync()

    assert client.get("/pack").status_code == 200
    assert sorted(calls) == sorted(shop["id"] for shop in cabinets.values())


def test_reloading_does_not_hammer_the_api(client, monkeypatch):
    """F5 не должен становиться запросом к площадке: обновляем не чаще интервала."""
    from app.core import sync as core_sync

    calls = []
    monkeypatch.setattr(core_sync, "sync_account",
                        lambda account, **kw: calls.append(account["id"]) or {})
    forget_sync()

    client.get("/pack")
    first = len(calls)
    client.get("/pack")
    client.get("/pack")
    assert len(calls) == first, f"лишние походы на площадку: {len(calls) - first}"


def test_the_filter_narrows_the_refresh_too(client, cabinets, monkeypatch):
    """Стоит фильтр — ходим только в его кабинеты, чужие не трогаем."""
    from app.core import sync as core_sync

    calls = []
    monkeypatch.setattr(core_sync, "sync_account",
                        lambda account, **kw: calls.append(account["id"]) or {})
    forget_sync()

    client.get("/pack?market=yandex")
    assert calls == [cabinets["yandex"]["id"]]


def test_a_broken_marketplace_does_not_break_the_page(client, monkeypatch):
    """Площадка отказала — рабочее место всё равно открывается."""
    from app.core import sync as core_sync

    def boom(account, **kw):
        raise RuntimeError("Ozon недоступен")

    monkeypatch.setattr(core_sync, "sync_account", boom)
    forget_sync()
    page = client.get("/pack")
    assert page.status_code == 200
    assert 'id="scan"' in page.text


def test_updated_at_is_the_marketplace_time(client):
    """«Обновлено» — время похода на площадку, а не опроса панели."""
    from app.core import sync as core_sync

    page = client.get("/pack").text
    stamp = core_sync.synced_at(1)
    assert stamp, "время синхронизации кабинета не записано"
    assert "Обновлено:" in page
    assert 'id="sync-time"' in page


# ------------------------------------------------------------------ шапка
def test_header_has_no_title_and_chips_go_left(client):
    """В шапке раздела только фильтр, прижатый к левому краю."""
    page = client.get("/pack").text
    head = page.split('id="label-gate"', 1)[0]
    assert "<h2" not in head.split('<div class="panel">', 1)[-1], "заголовок «Сборка» остался"
    assert "justify-content:flex-end" not in head, "чипы всё ещё прижаты вправо"


def test_refresh_button_says_orders(client, cabinets):
    """Кнопка обновляет заказы — про площадку в надписи ни слова."""
    for where in ("/pack", "/avito/pack", "/yandex/pack"):
        page = client.get(where).text
        assert "Обновить заказы" in page, where
        assert "Обновить из" not in page, where
