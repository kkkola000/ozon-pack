"""Avito — объявление площадки для реестра."""
from __future__ import annotations

from ..base import CatalogSource, LabelsSource, Market
from . import catalog, client, pack, returns, routes, store, sync

MARKET = Market(
    code="avito",
    title="Avito",
    id_label="client_id",
    key_label="client_secret",
    hint="Личный кабинет Avito → Настройки → Профиль → API",
    tables=("avito_orders", "avito_order_items"),
    router=routes.router,
    get_client=client.get_client,
    reset_client=client.reset_client,
    probe=client.probe,
    ping=lambda account: client.get_client(account).ping(),
    sync=sync.sync_account,
    stats=routes.settings_stats,
    schema=store.SCHEMA,
    raw_tables=("avito_orders",),
    labels=LabelsSource(
        word="этикетки",
        pending=pack.pending_labels,
        pdf=lambda account, user, keys: pack.labels(account, user, keys, mark=False)[0],
        table="avito_orders",
        key="id",
        size_hint="Размер этикетки выбирает Avito по службе доставки — 58×40 или 100×150. "
                  "Добавьте бумагу на оба: панель посмотрит размер файла и выберет строку.",
    ),
    # «Товары»: объявления кабинета — номер и название из /core/v1/items.
    catalog=CatalogSource(pages=catalog.pages),
    # Возврат у Avito — состояние заказа, но в разделе он выглядит как у всех.
    returns=returns.SOURCE,
    # «Заказы»: подтвердить, отправить, этикетки — в общем разделе.
    orders=routes.ORDERS,
    workspace=routes.WORKSPACE,
)
