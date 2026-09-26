"""Разделы без «текущего кабинета»: фильтр кабинетов на самой странице.

Переключателя в шапке больше нет, поэтому ни один раздел не должен от него
зависеть: что показать, решает фильтр (?shop=), а действие — кабинет строки.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app.core import accounts, db
from app.main import app
from app.markets.avito import client as avito
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
        page = test_client.get("/pack")
        test_client.headers["X-CSRF-Token"] = re.search(
            r'name="csrf-token" content="([^"]*)"', page.text).group(1)
        yield test_client


# ------------------------------------------------------------------ журнал
def test_journal_shows_every_cabinet_and_filters_by_one(client, cabinets):
    ozon, avito_shop = cabinets["ozon"], cabinets["avito"]
    db.log_event("label_print", account_id=ozon["id"], message="стикер Ozon")
    db.log_event("avito_label_print", account_id=avito_shop["id"], message="этикетка Avito")
    everything = client.get("/logs").text
    assert "стикер Ozon" in everything and "этикетка Avito" in everything
    # Колонка «Кабинет» — чьё это событие.
    assert "<th style=\"width:130px\">Кабинет</th>" in everything
    only = client.get(f"/logs?shop={avito_shop['id']}").text
    assert "этикетка Avito" in only and "стикер Ozon" not in only
    # Фильтр кабинета сохраняется вместе с остальными.
    assert f'name="shop" value="{avito_shop["id"]}"' in only


# ------------------------------------------------------------------ настройки
def test_settings_show_stats_in_every_cabinet_card(client, cabinets):
    page = client.get("/settings").text
    assert "текущий" not in page
    assert "Переключаются кабинеты списком в шапке" not in page
    cards = re.findall(r'<div class="cabinet-card[^"]*"\s+data-account="(\d+)"', page)
    assert {int(card) for card in cards} == {account["id"] for account in cabinets.values()}
    # Плитки площадки — в карточке её кабинета: «Отправлений» у Ozon, «Ждут сборки» у Маркета.
    assert "Отправлений:" in page and "Ждут сборки:" in page


def test_ozon_return_statuses_are_saved_whatever_the_cabinets(client, cabinets):
    """Статусы возвратов Ozon — общие на панель; сохраняются без кабинета Ozon в шапке."""
    response = client.post("/api/returns/statuses", json={"statuses": ["ArrivedAtReturnPlace"]})
    assert response.status_code == 200, response.text
    assert ozon_returns.get_returns_statuses() == ["ArrivedAtReturnPlace"]
    # Блок настроек Ozon показан: кабинет Ozon в панели есть.
    assert "/api/returns/statuses" in client.get("/settings").text


# ------------------------------------------------------------------ возвраты
def test_returns_sync_updates_the_filtered_cabinets(client, cabinets):
    everything = client.post("/api/returns/sync", json={"shop": "all"})
    assert everything.status_code == 200, everything.text
    assert "Ozon:" in everything.json()["message"] and "Avito:" in everything.json()["message"]
    one = client.post("/api/returns/sync", json={"shop": str(cabinets["avito"]["id"])})
    assert one.status_code == 200
    assert "Ozon" not in one.json()["message"]


def test_acts_for_all_cabinets_make_one_act_per_cabinet(client, cabinets):
    """Из поездки везут возвраты всех магазинов: акт — по одному на кабинет."""
    avito_shop = cabinets["avito"]
    row = db.query_one("SELECT id FROM avito_orders WHERE account_id = ? AND status = ?",
                       (avito_shop["id"], avito.STATUS_ON_RETURN))
    avito.get_client(avito_shop)._orders[row["id"]]["status"] = avito.STATUS_CLOSED
    avito_sync.sync_avito(avito_shop)
    day = db.query_one("SELECT received_day FROM avito_orders WHERE id = ?", (row["id"],))["received_day"]

    preview = client.post("/api/returns/acts/by-day", json={"day": day, "dry_run": True, "shop": "all"}).json()
    assert preview["found"] >= 1
    made = client.post("/api/returns/acts/by-day", json={"day": day, "shop": "all"}).json()
    assert made["act_id"], made
    acts = db.query("SELECT account_id FROM return_acts WHERE confirmed_at IS NULL")
    assert avito_shop["id"] in {act["account_id"] for act in acts}
    # На вкладке — акт с возвратом Avito, отметка несёт кабинет строки.
    page = client.get("/returns?tab=acts").text
    assert f'data-account="{avito_shop["id"]}"' in page


def test_a_market_without_returns_has_no_chip(client, cabinets):
    page = client.get("/returns").text
    assert f'/returns?shop={cabinets["avito"]["id"]}' in page
    assert f'/returns?shop={cabinets["yandex"]["id"]}"' not in page
