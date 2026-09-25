"""Avito — объявление площадки для реестра."""
from __future__ import annotations

from ..base import LabelsSource, Market
from . import client, pack, returns, routes, store, sync

MARKET = Market(
    code="avito",
    title="Avito",
    id_label="client_id",
    key_label="client_secret",
    hint="Личный кабинет Avito → Настройки → Профиль → API",
    # Заказы — в общем разделе «/orders»; своё у Avito — рабочее место.
    home="/avito/pack",
    prefixes=("/avito",),
    tables=("avito_orders", "avito_order_items"),
    router=routes.router,
    get_client=client.get_client,
    reset_client=client.reset_client,
    probe=client.probe,
    ping=lambda account: client.get_client(account).ping(),
    sync=sync.sync_account,
    nav=routes.nav_items,
    stats=routes.settings_stats,
    schema=store.SCHEMA,
    raw_tables=("avito_orders",),
    labels=LabelsSource(
        word="этикетки",
        pending=pack.pending_labels,
        pdf=pack.labels_pdf,
        table="avito_orders",
        key="id",
        size_hint="Размер этикетки зависит от службы доставки — бывает 58×40 и 100×150. "
                  "Добавьте оба размера: панель посмотрит размер файла и выберет принтер.",
    ),
    # Возврат у Avito — состояние заказа, но в разделе он выглядит как у всех.
    returns=returns.SOURCE,
    orders_feed=store.orders_feed,
    # «Заказы»: подтвердить, отправить, этикетки — в общем разделе.
    orders=routes.ORDERS,
    workspace=routes.WORKSPACE,
)
