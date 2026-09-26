"""«Возвраты»: сверху кабинеты и статус, ниже — общие фильтры списка.

Фильтры (поиск, пункт выдачи, FBO/FBS) одни на все кабинеты и работают и под
«Все кабинеты». К ним привязано всё: строки, числа на чипах и статусах, лист
печати и PDF, — на бумаге ровно то, что на экране.
"""
import re
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.core import accounts, db
from app.main import app
from app.markets.avito import client as avito
from app.markets.avito import returns as avito_returns
from app.markets.avito import sync as avito_sync
from app.markets.ozon import returns as ozon_returns


@pytest.fixture
def cabinets(sample_data, avito_account, yandex_account):
    avito_sync.sync_avito(avito_account)
    return {"ozon": accounts.default_account(), "avito": avito_account, "yandex": yandex_account}


@pytest.fixture
def client(cabinets):
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.post("/login", data={"login": "admin", "password": "test-admin-pass", "next": "/pack"})
        yield test_client


def _ids(sql: str, *params) -> set[str]:
    return {str(row["id"]) for row in db.query(sql, params)}


def _ozon(scheme: str) -> set[str]:
    return _ids("SELECT id FROM returns WHERE is_ready = 1 AND scheme = ?", scheme)


def _avito_ready(account_id: int) -> list[dict]:
    return [dict(row) for row in db.query(
        "SELECT id, marketplace_id, terminal_address FROM avito_orders "
        "WHERE account_id = ? AND status = ? AND received_at IS NULL",
        (account_id, avito.STATUS_ON_RETURN))]


def _chips(page: str) -> dict[str, int]:
    """Чипы кабинетов: название -> число на чипе."""
    block = page[page.index('id="shop-chips"'):page.index('id="status-tabs"')]
    return {title.strip(): int(count) for title, count in
            re.findall(r'<a class="chip[^>]*>(?:<i class="dot [a-z]+"></i>)?\s*([^<]+?)\s*'
                       r'<span class="badge">(\d+)</span>', block)}


def _statuses(page: str) -> dict[str, int]:
    block = page[page.index('id="status-tabs"'):]
    block = block[:block.index("</div>")]
    return {title.strip(): int(count) for title, count in
            re.findall(r'>\s*([^<>]+?)\s*<span class="badge[^"]*">(\d+)</span>', block)}


def _blocks(page: str) -> list[str]:
    return [title.strip() for title in re.findall(r'class="returns-shop">\s*<i class="dot [a-z]+"></i>([^<]+)<', page)]


# ------------------------------------------------------------------ статус сверху
def test_status_row_sits_under_cabinets_with_cross_counts(client, cabinets):
    ozon_ready = len(_ozon("FBO") | _ozon("FBS"))
    avito_ready = len(_avito_ready(cabinets["avito"]["id"]))
    assert ozon_ready and avito_ready, "в демо-данных нужны возвраты обеих площадок"

    page = client.get("/returns").text
    top = page[:page.index('id="returns-panel"')]
    assert 'id="shop-chips"' in top and 'id="status-tabs"' in top, "строка «Статус» — в верхней панели"
    assert _statuses(page) == {"К выдаче": ozon_ready + avito_ready, "Ждёт подтверждения": 0}
    # На кабинете — сколько у него в выбранном статусе.
    assert _chips(page) == {"Все кабинеты": ozon_ready + avito_ready, "Ozon": ozon_ready, "Avito": avito_ready}

    # Статус считается под выбранным кабинетом.
    one = client.get(f"/returns?shop={cabinets['avito']['id']}").text
    assert _statuses(one)["К выдаче"] == avito_ready


def test_acts_status_counts_acts_on_chips(client, cabinets):
    avito_shop = cabinets["avito"]
    row = db.query_one("SELECT id FROM avito_orders WHERE account_id = ? AND status = ?",
                       (avito_shop["id"], avito.STATUS_ON_RETURN))
    avito.get_client(avito_shop)._orders[row["id"]]["status"] = avito.STATUS_CLOSED
    avito_sync.sync_avito(avito_shop)
    day = db.query_one("SELECT received_day FROM avito_orders WHERE id = ?", (row["id"],))["received_day"]
    page = client.get("/pack").text
    csrf = re.search(r'name="csrf-token" content="([^"]*)"', page).group(1)
    made = client.post("/api/returns/acts/by-day", json={"day": day, "shop": "all"},
                       headers={"X-CSRF-Token": csrf})
    assert made.json()["act_id"], made.text

    acts = client.get("/returns?tab=acts").text
    assert _statuses(acts)["Ждёт подтверждения"] == 1
    assert _chips(acts) == {"Все кабинеты": 1, "Ozon": 0, "Avito": 1}
    # Статус «К выдаче» при этом считает своё — ссылка ведёт обратно на список.
    assert 'href="/returns?shop=all"' in acts


# ------------------------------------------------------------------ фильтры под «Все кабинеты»
def test_scheme_filter_works_across_every_cabinet(client, cabinets):
    fbs, fbo = _ozon("FBS"), _ozon("FBO")
    page = client.get("/returns?shop=all&scheme=FBS").text

    for return_id in fbs:
        assert return_id in page, return_id
    for return_id in fbo:
        assert return_id not in page, f"FBO {return_id} под «Только FBS»"
    # У Avito схемы нет: под FBS его строк нет, пустой блок не показываем.
    assert _blocks(page) == ["Ozon"]
    assert _chips(page) == {"Все кабинеты": len(fbs), "Ozon": len(fbs), "Avito": 0}
    assert _statuses(page)["К выдаче"] == len(fbs)
    assert f"Печать листа ({len(fbs)})" in page
    # Лист и PDF — с тем же фильтром.
    assert 'href="/returns/print?shop=all&amp;scheme=FBS"' in page
    assert 'data-print-pdf="/returns/sheet.pdf?shop=all&amp;scheme=FBS"' in page
    # Фильтр держится при смене кабинета и статуса.
    assert f'href="/returns?shop={cabinets["ozon"]["id"]}&amp;scheme=FBS"' in page
    assert 'href="/returns?shop=all&amp;tab=acts&amp;scheme=FBS"' in page


def test_place_filter_spans_both_markets(client, cabinets):
    avito_rows = _avito_ready(cabinets["avito"]["id"])
    address = avito_rows[0]["terminal_address"]
    ozon_place = db.query_one("SELECT place_name FROM returns WHERE is_ready = 1")["place_name"]

    page = client.get("/returns").text
    # Варианты — пункты выдачи всех кабинетов под фильтром.
    assert f'<option value="{ozon_place}"' in page and f'<option value="{address}"' in page

    only = client.get(f"/returns?shop=all&place={quote(address)}").text
    assert _blocks(only) == ["Avito"]
    for row in avito_rows:
        if row["terminal_address"] == address:
            assert row["marketplace_id"] in only
    assert not any(return_id in only for return_id in _ozon("FBO") | _ozon("FBS"))
    assert f'<option value="{address}" selected>' in only


def test_search_finds_a_return_in_any_cabinet(client, cabinets):
    number = _avito_ready(cabinets["avito"]["id"])[0]["marketplace_id"]
    page = client.get(f"/returns?shop=all&q={number}").text
    assert _blocks(page) == ["Avito"]
    assert number in page
    assert _statuses(page)["К выдаче"] == 1
    # «Только этот кабинет» уводит с тем же поиском.
    assert f'href="/returns?shop={cabinets["avito"]["id"]}&amp;q={number}"' in page

    nothing = client.get("/returns?shop=all&q=zzz-нет-такого").text
    assert "Под выбранными фильтрами возвратов нет." in nothing
    assert "Сбросить" in nothing


def test_scheme_select_only_where_it_means_something(client, cabinets):
    assert 'name="scheme"' in client.get("/returns").text
    assert 'name="scheme"' in client.get(f"/returns?shop={cabinets['ozon']['id']}").text
    assert 'name="scheme"' not in client.get(f"/returns?shop={cabinets['avito']['id']}").text
    # Пришли на Avito с выбранной схемой — выбор виден, иначе пустой список не объяснить.
    stuck = client.get(f"/returns?shop={cabinets['avito']['id']}&scheme=FBS").text
    assert '<option value="FBS" selected>' in stuck
    assert "Под выбранными фильтрами возвратов нет." in stuck


# ------------------------------------------------------------------ лист
def test_print_sheet_follows_the_place_filter(client, cabinets):
    address = _avito_ready(cabinets["avito"]["id"])[0]["terminal_address"]
    sheet = client.get(f"/returns/print?shop=all&place={quote(address)}").text
    assert re.findall(r'<h2 class="section">([^<]+)</h2>', sheet) == ["Avito"]
    assert re.search(rf"все кабинеты\s*·\s*{re.escape(address)}", sheet), "фильтр не подписан на листе"


def test_sheet_pdf_follows_the_filters(client, cabinets):
    from io import BytesIO

    from pypdf import PdfReader

    response = client.get("/returns/sheet.pdf?shop=all&scheme=FBO")
    assert response.status_code == 200, response.text
    text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(response.content)).pages)
    assert "только FBO" in text
    for return_id in _ozon("FBS"):
        assert return_id not in text, f"FBS {return_id} в PDF «только FBO»"


# ------------------------------------------------------------------ площадки
def test_market_counts_match_their_lists(cabinets):
    """Число на чипе и список считаются одним условием — расходиться им не с чего."""
    ozon_id, avito_id = cabinets["ozon"]["id"], cabinets["avito"]["id"]
    for params in ({}, {"scheme": "FBS"}, {"scheme": "FBO"}, {"q": "zzz"},
                   {"place": "ПВЗ Москва, Ленинский 25"}):
        assert ozon_returns.count_ready([ozon_id], params) == len(ozon_returns.ready([ozon_id], params)), params
    address = _avito_ready(avito_id)[0]["terminal_address"]
    for params in ({}, {"place": address}, {"place": "нет такого"}, {"q": "zzz"}):
        assert avito_returns.count_ready([avito_id], params) == len(avito_returns.ready([avito_id], params)), params
    assert avito_returns.places([avito_id]) == sorted({row["terminal_address"] for row in _avito_ready(avito_id)})
