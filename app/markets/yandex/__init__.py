"""Яндекс Маркет — объявление площадки для реестра.

В настройках только один идентификатор — кабинета (businessId). Идентификатор
магазина (campaignId) панель не спрашивает: он приходит в каждом заказе, и
заводить его руками значило бы просить то, что и так есть.
"""
from __future__ import annotations

from ..base import CatalogSource, LabelsSource, Market
from . import catalog, client, pack, routes, store, sync

MARKET = Market(
    code="yandex",
    title="Яндекс Маркет",
    id_label="businessId",
    key_label="Api-Key",
    hint="Кабинет Маркета → Настройки → API и модули → Токены авторизации. "
         "Доступы: обработка заказов и, для раздела «Товары», управление товарами и карточками",
    tables=("yandex_orders", "yandex_order_items"),
    router=routes.router,
    get_client=client.get_client,
    reset_client=client.reset_client,
    probe=client.probe,
    ping=lambda account: client.get_client(account).ping(),
    sync=sync.sync_account,
    stats=routes.settings_stats,
    schema=store.SCHEMA,
    raw_tables=("yandex_orders",),
    labels=LabelsSource(
        word="ярлыки",
        pending=pack.pending_labels,
        pdf=lambda account, user, keys: pack.labels(account, user, keys, mark=False)[0],
        table="yandex_orders",
        key="id",
        size_hint="Выбранный размер — это и формат, в котором панель запрашивает ярлык у Маркета: "
                  "58×40 или 75×120.",
    ),
    # Каталог со штрихкодами Маркет отдаёт одним методом — раздел «Товары»
    # открывается и его кабинетам.
    catalog=CatalogSource(pages=catalog.pages),
    # «Заказы»: ярлыки и снятие отметки — в общем разделе.
    orders=routes.ORDERS,
    workspace=routes.WORKSPACE,
)
