"""Ozon — объявление площадки для реестра.

Ozon умеет больше остальных: отдаёт каталог товаров со штрихкодами, возвраты
отдельным методом и переводит отправления в «Ожидает отгрузки». Поэтому его
пакет самый толстый, но состав файлов тот же, что у других площадок.
"""
from __future__ import annotations

from ...core.config import settings
from ..base import Market
from . import client, routes, sync

MARKET = Market(
    code="ozon",
    title="Ozon",
    id_label="Client-Id",
    key_label="Api-Key",
    hint="Личный кабинет Ozon → Настройки → Seller API",
    home="/pack",
    prefixes=("/pack", "/orders", "/returns", "/api/"),
    tables=("postings", "posting_items", "products", "product_barcodes", "returns"),
    router=routes.router,
    get_client=client.get_client,
    reset_client=client.reset_client,
    probe=client.probe,
    ping=lambda account: client.get_client(account).ping(),
    sync=sync.sync_account,
    nav=routes.nav_items,
    stats=routes.settings_stats,
    # Первый кабинет Ozon исторически мог получать ключи из .env.
    env_credentials=lambda: (settings.ozon_client_id, settings.ozon_api_key),
)
