"""Подделки клиентов площадок — по файлу на площадку.

Панель работает только на данных площадок, поэтому в тестах вместо сети стоят
эти подделки: те же классы клиентов, но с заранее заданными заказами, товарами
и возвратами. Подстановка одна на все площадки — install(), её зовёт conftest.

Наборы данных доступны и отсюда: тест пишет «from tests.fakes import
CATALOG_EXTRA», не разбираясь, в каком файле это лежит.
"""
from __future__ import annotations

from app.core import accounts
# Клиенты площадок — под своими именами: короткие «ozon», «avito», «yandex»
# здесь уже заняты одноимёнными файлами подделок, и подмена уходила бы в них.
from app.markets.avito import client as avito_client
from app.markets.ozon import client as ozon_client
from app.markets.yandex import client as yandex_client

from tests.fakes.avito import (SAMPLE_BUYERS, SAMPLE_ITEMS, SAMPLE_SERVICES,
                               FakeAvitoClient)
from tests.fakes.ozon import (CATALOG_ARCHIVED, CATALOG_EXTRA, SAMPLE_CITIES,
                              SAMPLE_PRODUCTS, FakeOzonClient)
from tests.fakes.yandex import YANDEX_DELIVERY, FakeYandexClient

__all__ = [
    "CATALOG_ARCHIVED", "CATALOG_EXTRA", "FakeAvitoClient", "FakeOzonClient",
    "FakeYandexClient", "SAMPLE_BUYERS", "SAMPLE_CITIES", "SAMPLE_ITEMS",
    "SAMPLE_PRODUCTS", "SAMPLE_SERVICES", "YANDEX_DELIVERY", "install",
]


def install(monkeypatch) -> None:
    """Подменить фабрики клиентов на подделки — на время одного теста."""
    ozon_clients: dict[int, FakeOzonClient] = {}
    avito_clients: dict[int, FakeAvitoClient] = {}
    yandex_clients: dict[int, FakeYandexClient] = {}

    def fake_ozon(account: dict | None = None) -> FakeOzonClient:
        if account is None:
            account = accounts.default_account()
        account_id = int((account or {}).get("id") or 0)
        return ozon_clients.setdefault(account_id, FakeOzonClient(seed=account_id))

    def fake_avito(account: dict | None = None) -> FakeAvitoClient:
        account_id = int((account or {}).get("id") or 0)
        return avito_clients.setdefault(account_id, FakeAvitoClient(seed=account_id))

    def fake_yandex(account: dict | None = None) -> FakeYandexClient:
        account_id = int((account or {}).get("id") or 0)
        return yandex_clients.setdefault(account_id, FakeYandexClient(seed=account_id))

    monkeypatch.setattr(ozon_client, "get_client", fake_ozon)
    monkeypatch.setattr(avito_client, "get_client", fake_avito)
    monkeypatch.setattr(yandex_client, "get_client", fake_yandex)
