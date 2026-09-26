"""Ozon — объявление площадки для реестра.

Ozon умеет больше остальных: отдаёт каталог товаров со штрихкодами, возвраты
отдельным методом и переводит отправления в «Ожидает отгрузки». Поэтому его
пакет самый толстый, но состав файлов тот же, что у других площадок.
"""
from __future__ import annotations

from ...core.config import settings
from ..base import CatalogSource, LabelsSource, Market
from . import catalog, client, migrations, pack, returns, routes, store, sync

MARKET = Market(
    code="ozon",
    title="Ozon",
    id_label="Client-Id",
    key_label="Api-Key",
    hint="Личный кабинет Ozon → Настройки → Seller API",
    # Каталог (products, product_barcodes) здесь не числится: таблица общая,
    # её чистит ядро вместе с кабинетом любой площадки.
    tables=("postings", "posting_items", "returns"),
    router=routes.router,
    get_client=client.get_client,
    reset_client=client.reset_client,
    probe=client.probe,
    ping=lambda account: client.get_client(account).ping(),
    sync=sync.sync_account,
    stats=routes.settings_stats,
    # Первый кабинет Ozon исторически мог получать ключи из .env.
    env_credentials=lambda: (settings.ozon_client_id, settings.ozon_api_key),
    schema=store.SCHEMA + returns.SCHEMA,
    raw_tables=("postings", "returns"),
    migrate=migrations.migrate,
    # Стикеры: выгружаются до сборки, вместе со стикерами других кабинетов.
    labels=LabelsSource(
        word="стикеры",
        pending=pack.pending_labels,
        pdf=lambda account, user, keys: pack.label_pdf(account, user, keys)[0],
        table="postings",
        key="posting_number",
        size_hint="Размер стикера задаётся в личном кабинете Ozon — панель печатает файл как есть.",
    ),
    # Каталог со штрихкодами: список артикулов, потом карточки пачками.
    catalog=CatalogSource(pages=catalog.pages),
    # Возвраты Ozon: отдельная сущность со своим методом API и своими статусами.
    returns=returns.SOURCE,
    orders_feed=store.orders_feed,
    # «Заказы»: какие отправления в каком статусе склада и что с ними можно сделать.
    orders=routes.ORDERS,
    workspace=routes.WORKSPACE,
    settings_rows="ozon/settings_rows.html",
    settings_panel="ozon/settings_panel.html",
    settings_context=lambda _account: {
        "returns_statuses": returns.get_returns_statuses(),
        "returns_choices": returns.RETURN_STATUS_CHOICES,
        "returns_source": returns.returns_source(),
        "received_statuses": returns.get_received_statuses(),
        "received_days": returns.get_received_days(),
    },
)
