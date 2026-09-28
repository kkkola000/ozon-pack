"""«Сборка»: поле с очередью наверху, сборка — во всплывающем окне.

На странице от зоны сканирования остались значок, поле, «Сканер готов» и
строка очереди. Сборка открытого заказа идёт во всплывающем окне, и поле
переезжает туда. Порядок страницы: шапка → поле с очередью → «Все заказы» →
«Последние сканы».

Рамку, состояния, окно и «что дальше» рисует market_pack.js, а слова и
содержимое окна — площадка: под фильтром кабинета поле говорит словами его
площадки, под «Все заказы» — общими.
"""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import accounts
from app.main import app
from app.markets import registry

STATIC = Path(__file__).resolve().parents[2] / "app"


@pytest.fixture
def cabinets(sample_data, avito_account, yandex_account):
    return {"ozon": accounts.default_account(), "avito": avito_account, "yandex": yandex_account}


@pytest.fixture
def client(cabinets):
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/pack"})
        yield test_client


def _zone(page: str) -> str:
    start = page.index('<div class="scan-zone compact" id="scan-panel"')
    return page[start:page.index("Все заказы</h2>")]


def _modal(page: str) -> str:
    return page[page.index('id="pack-modal"'):]


def test_zone_is_the_field_and_the_queue(client):
    zone = _zone(client.get("/pack").text)
    for element in ('id="scan"', 'id="banner"', 'id="scan-refocus"', 'id="scan-state"', 'id="btn-clear"',
                    'id="queue"', 'id="btn-sync"', 'id="btn-sheet"', 'id="sync-time"'):
        assert element in zone, element
    assert "Сканер готов" in zone
    # Сборка ушла в окно: на странице нет ни «что сканировать», ни карточки заказа.
    assert "Что сканировать" not in zone
    for gone in ('id="scan-left"', 'id="scan-steps"', 'id="scan-title"', 'id="active-panel"', 'id="idle-panel"'):
        assert gone not in client.get("/pack").text, gone


def test_queue_tiles_live_in_the_zone(client):
    zone = _zone(client.get("/pack").text)
    queue = zone[zone.index('id="queue"'):]
    assert "К сборке" in queue and "Горит сегодня" in queue and "Собрано сегодня" in queue
    assert queue.count('data-key="') == 3


def test_page_order(client):
    """Шапка → поле с очередью → «Все заказы» → «Последние сканы»."""
    page = client.get("/pack").text
    assert (page.index('id="scan-panel"') < page.index("Все заказы</h2>")
            < page.index("Последние сканы</h2>") < page.index('id="pack-modal"'))


def test_packing_window_is_on_the_page_and_closed(client):
    modal = _modal(client.get("/pack").text)
    assert modal.startswith('id="pack-modal" hidden')
    for element in ('id="pack-number"', 'id="pack-actions"', 'id="pack-scan"', 'id="pack-slot"',
                    'id="pack-title"', 'id="pack-items"', 'id="pack-foot"', 'id="pack-done"'):
        assert element in modal, element
    # Крестика нет: окно закрывается, когда заказ собран или сборку отменили.
    assert "✕</button>" not in modal.split('id="pack-slot"')[0]


def test_all_orders_speak_common_words(client):
    zone = _zone(client.get("/pack").text)
    assert 'placeholder="Сканируйте товар или наклейку заказа…"' in zone


@pytest.mark.parametrize("market, title", [
    ("ozon", "Сканируйте товар или стикер отправления"),
    ("avito", "Сканируйте стикер отправления"),
    ("yandex", "Сканируйте товар или ярлык заказа"),
])
def test_cabinet_filter_speaks_its_market_words(client, cabinets, market, title):
    zone = _zone(client.get(f"/pack?shop={cabinets[market]['id']}").text)
    assert f'placeholder="{title}…"' in zone


def test_every_workspace_declares_its_scan_words():
    for market in registry.all_markets():
        workspace = market.workspace
        if workspace is None:
            continue
        assert workspace.title and workspace.hint, market.code


def test_every_pack_fills_the_window():
    """Окно рисует ядро, а что в нём — площадка открытого заказа."""
    for market in registry.all_markets():
        if market.workspace is None:
            continue
        script = (STATIC / "markets" / market.code / "static" / "pack.js").read_text(encoding="utf-8")
        assert re.search(r"\bcard\(state\)", script), f"{market.code}: нет card(state)"
        assert "renderActive" not in script, f"{market.code}: осталась старая карточка на странице"
        assert re.search(r"\bleft[(:]", script), f"{market.code}: нет left(state)"
        assert "number:" in script, f"{market.code}: нет number(active)"
        assert re.search(r"\btitle: '", script), f"{market.code}: нет названия площадки для окна"
        assert re.search(r"\bclose: '", script), f"{market.code}: нет words.close"
        assert re.search(r"\bdone: '", script), f"{market.code}: нет words.done для «Собрано»"
