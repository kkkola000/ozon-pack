"""«Сборка» — один раздел на все площадки: фильтр кабинетов и общий обработчик.

Рабочее место открывается по адресу любой площадки, но страница одна: её
собирает routes/pack.py, а площадка объявляет только слова, плитки очереди,
как сканировать и чей это код.

Кабинет в шапке тут ничего не решает. Решает фильтр: «Все заказы» — сборка по
всем кабинетам, выбран кабинет — в его границах. Фильтр по кабинетам, а не по
площадкам: у Ozon их бывает два, и склад собирает их раздельно. Кабинет в
шапке при этом не переключается: страница про это больше не знает.
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
    """Четыре кабинета на трёх площадках — у Ozon их два. У всех свои заказы."""
    second = accounts.get(accounts.create("ozon", "Второй склад", "test-client", "test-key"))
    sync.sync_all()
    avito_one = accounts.get(accounts.create("avito", "Магазин Avito", "test-client", "test-secret"))
    avito_sync.sync_avito(avito_one)
    yandex_one = accounts.get(accounts.create("yandex", "Маркет", "9000001", "test-token"))
    yandex_sync.sync_yandex(yandex_one)
    return {"ozon": accounts.default_account(), "ozon2": second, "avito": avito_one, "yandex": yandex_one}


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


def rows_of(page: str) -> list[str]:
    """Площадки строк списка «Все заказы»."""
    return re.findall(r'<tr data-work="\d" data-market="([^"]+)"', page)


def shops_of(page: str) -> list[int]:
    """Кабинеты строк списка «Все заказы»."""
    return [int(n) for n in re.findall(r'<tr data-work="\d" data-market="[^"]+" data-shop="(\d+)"', page)]


# ------------------------------------------------------------------ один обработчик
def test_old_market_urls_lead_to_the_one_pack(client, cabinets):
    """Адрес «Сборки» один — /pack. Старые адреса площадок ведут туда же, с фильтром."""
    yandex = cabinets["yandex"]["id"]
    for old in ("/avito/pack", "/yandex/pack"):
        assert client.get(old).headers["location"] == "/pack"
        assert client.get(f"{old}?shop={yandex}").headers["location"] == f"/pack?shop={yandex}"
    page = client.get("/pack")
    assert page.status_code == 200, page.text[:300]
    assert 'id="scan"' in page.text
    assert "Все заказы" in page.text
    assert len(set(rows_of(page.text))) > 1, "фильтр по умолчанию — «Все заказы»"


def test_market_declares_its_workspace(client):
    """Площадка отдаёт слова и два числа — остальное собирает общий маршрут."""
    for market in registry.all_markets():
        workspace = market.workspace
        assert workspace is not None, market.code
        assert callable(workspace.load_state) and callable(workspace.count_queue)
        # Скан площадка тоже объявляет: ядро не ходит в её ручки напрямую.
        assert callable(workspace.owner) and callable(workspace.scan)
        assert callable(workspace.release)


# ------------------------------------------------------------------ фильтр кабинетов
def test_filter_has_a_chip_per_cabinet(client, cabinets):
    """Чипы: «Все заказы» и по одному на каждый кабинет, заведённый в панель."""
    page = client.get("/pack").text
    assert "Все заказы" in page
    for shop in cabinets.values():
        assert f'?shop={shop["id"]}"' in page, shop["title"]
        assert shop["title"] in page, shop["title"]


def test_two_cabinets_of_one_marketplace_are_two_chips(client, cabinets):
    """Два кабинета Ozon — два чипа, а не один «Ozon» на оба.

    Ради этого фильтр и переделан: склад собирает магазины раздельно, и
    объединять их по площадке значило бы смешать чужие заказы со своими.
    """
    page = client.get("/pack").text
    chips = page.split('<div class="chips">', 1)[1].split("</div>", 1)[0]
    assert chips.count('class="dot ozon"') == 2
    assert f'?shop={cabinets["ozon"]["id"]}"' in chips and f'?shop={cabinets["ozon2"]["id"]}"' in chips


def test_filter_leaves_only_its_cabinet(client, cabinets):
    """Выбран кабинет — в списке только его заказы, даже без соседа той же площадки."""
    everything = shops_of(client.get("/pack").text)
    assert cabinets["ozon"]["id"] in everything and cabinets["ozon2"]["id"] in everything

    only_second = shops_of(client.get(f"/pack?shop={cabinets['ozon2']['id']}").text)
    assert only_second and set(only_second) == {cabinets["ozon2"]["id"]}
    assert len(only_second) < len(everything)


def test_filter_narrows_to_the_cabinet(client, cabinets):
    """Выбрали кабинет — список его: границы сборки задаёт фильтр."""
    avito = cabinets["avito"]["id"]
    page = client.get(f"/pack?shop={avito}")
    assert page.status_code == 200, page.text[:300]
    assert set(shops_of(page.text)) == {avito}
    # И чип ведёт на тот же адрес «Сборки».
    assert f'"/pack?shop={avito}"' in page.text


def test_all_orders_is_the_default(client):
    """Без фильтра — весь список: это и есть «объединённая сборка»."""
    page = client.get("/pack").text
    assert len(set(rows_of(page))) > 1
    assert 'class="chip active"' in page


def test_counts_on_chips_are_for_the_whole_list(client, cabinets):
    """Число на чипе — сколько всего у кабинета, а не сколько осталось после фильтра."""
    everything = shops_of(client.get("/pack").text)
    filtered = client.get(f"/pack?shop={cabinets['ozon']['id']}").text
    for shop in cabinets.values():
        count = everything.count(shop["id"])
        chip = filtered.split(f'?shop={shop["id"]}"', 1)[1].split("</a>", 1)[0]
        assert f'<span class="badge">{count}</span>' in chip, shop["title"]


def test_unknown_filter_is_not_a_crash(client):
    """Кривой адрес не ломает рабочее место — показываем всё.

    Сюда же — старые ссылки с фильтром по площадке (?market=ozon): такого
    фильтра больше нет, и они открывают «Все заказы».
    """
    for where in ("/pack?shop=999999", "/pack?shop=нет-такого", "/pack?market=ozon"):
        page = client.get(where)
        assert page.status_code == 200, where
        assert len(set(rows_of(page.text))) > 1, where


def test_a_cabinet_without_keys_has_no_chip(client):
    """Кабинет без ключей в фильтре не показываем: собирать в нём нечего."""
    empty = accounts.get(accounts.create("ozon", "Пустой кабинет"))
    page = client.get("/pack").text
    assert f'?shop={empty["id"]}"' not in page
    # И по прямой ссылке он не открывается как фильтр — это «Все заказы».
    assert len(set(rows_of(client.get(f"/pack?shop={empty['id']}").text))) > 1


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

    client.get(f"/pack?shop={cabinets['yandex']['id']}")
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
    page = client.get("/pack").text
    assert "Обновить заказы" in page
    assert "Обновить из" not in page
