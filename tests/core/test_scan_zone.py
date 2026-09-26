"""Зона сканирования на «Сборке»: что делать, что подносить к сканеру, куда смотреть.

Рамку, состояния и оранжевый список оставшегося рисует market_pack.js, а
слова — площадка: под фильтром кабинета зона говорит словами его площадки,
под «Все заказы» — общими.
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
    start = page.index('<div class="scan-zone" id="scan-panel"')
    return page[start:page.index('<div id="active-panel">')]


def _kinds(zone: str) -> list[str]:
    return [" ".join(word.split()) for word in re.findall(r'<span class="scan-kind"><svg.*?</svg>\s*([^<]+)</span>',
                                                        zone, flags=re.S)]


def test_zone_replaces_the_old_scan_bar(client):
    page = client.get("/pack").text
    assert 'class="scanbar"' not in page
    zone = _zone(page)
    # Поле, итог скана, кнопка «вернуть фокус» и место под оставшиеся товары — в одной рамке.
    for element in ('id="scan"', 'id="banner"', 'id="scan-refocus"', 'id="scan-left"', 'id="scan-steps"',
                    'id="scan-state"', 'id="btn-clear"'):
        assert element in zone, element
    assert "Сканер готов" in zone
    assert "Нет сканера — введите код и нажмите" in zone


def test_all_orders_speak_common_words(client):
    zone = _zone(client.get("/pack").text)
    assert 'data-title="Сканируйте товар или наклейку заказа"' in zone
    assert _kinds(zone) == ["Штрихкод товара", "Наклейка заказа"]


@pytest.mark.parametrize("market, title, kinds", [
    ("ozon", "Сканируйте товар или стикер отправления", ["Штрихкод товара", "Стикер отправления"]),
    # У Avito сборку открывает стикер — он и идёт первым.
    ("avito", "Сканируйте стикер отправления", ["Стикер отправления", "Штрихкод товара"]),
    ("yandex", "Сканируйте товар или ярлык заказа", ["Штрихкод товара", "Ярлык заказа"]),
])
def test_cabinet_filter_speaks_its_market_words(client, cabinets, market, title, kinds):
    zone = _zone(client.get(f"/pack?shop={cabinets[market]['id']}").text)
    assert f'id="scan-title">{title}<' in zone
    assert _kinds(zone) == kinds


def test_every_workspace_declares_its_scan_words():
    for market in registry.all_markets():
        workspace = market.workspace
        if workspace is None:
            continue
        assert workspace.title and workspace.hint, market.code
        assert workspace.kinds and all(kind in ("product", "label") for kind, _word in workspace.kinds), market.code


def test_every_pack_card_tells_what_is_left():
    """Оранжевый список и заголовок «что дальше» ядро берёт у площадки открытого заказа."""
    for market in registry.all_markets():
        if market.workspace is None:
            continue
        script = (STATIC / "markets" / market.code / "static" / "pack.js").read_text(encoding="utf-8")
        assert re.search(r"\bleft[(:]", script), f"{market.code}: нет left(state)"
        assert "number:" in script, f"{market.code}: нет number(active)"
        assert re.search(r"\bclose: '", script), f"{market.code}: нет words.close"
