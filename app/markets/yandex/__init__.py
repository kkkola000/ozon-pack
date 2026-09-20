"""Яндекс Маркет — объявление площадки для реестра.

В настройках только один идентификатор — кабинета (businessId). Идентификатор
магазина (campaignId) панель не спрашивает: он приходит в каждом заказе, и
заводить его руками значило бы просить то, что и так есть.
"""
from __future__ import annotations

from ..base import Market
from . import client, routes, sync

MARKET = Market(
    code="yandex",
    title="Яндекс Маркет",
    id_label="businessId",
    key_label="Api-Key",
    hint="Кабинет Маркета → Настройки → API и модули → Токены авторизации",
    home="/yandex/pack",
    prefixes=("/yandex",),
    tables=("yandex_orders", "yandex_order_items"),
    router=routes.router,
    get_client=client.get_client,
    reset_client=client.reset_client,
    probe=client.probe,
    ping=lambda account: client.get_client(account).ping(),
    sync=sync.sync_account,
    nav=routes.nav_items,
    stats=routes.settings_stats,
)
