"""Каталог Avito: объявления кабинета — это и есть его товары.

Метод один — «Получение информации по объявлениям» (GET /core/v1/items):
страницы по 99 объявлений, в каждом номер, название, цена, статус и ссылка.
Других методов Avito ради каталога панель не вызывает.

В каталог берутся только активные объявления, из каждого — номер и название.
Номер объявления служит и артикулом карточки. Фото и штрихкодов этот метод не
отдаёт, и панель их не придумывает: карточка Avito в «Товарах» — артикул
(номер объявления) и название.
"""
from __future__ import annotations

import time
from collections.abc import Iterator

from ...core.store import _text
from ..base import CatalogPage
from . import client as avito

# Не больше 25 запросов в минуту: между страницами — пауза с запасом.
PAUSE = 2.5
# Потолок страниц: 500 по 99 — почти 50 тысяч объявлений. Без него ошибка
# площадки в нумерации крутила бы обход бесконечно.
MAX_PAGES = 500


def card(resource: dict) -> dict | None:
    """Объявление в общем виде каталога. Без номера — не карточка."""
    item_id = str(resource.get("id") or "").strip()
    if not item_id:
        return None
    # Номер объявления — он же артикул: другого у Avito в этом методе нет.
    return {"sku": item_id, "offer_id": item_id, "name": _text(resource.get("title")), "image": None,
            "barcodes": []}


def pages(account: dict) -> Iterator[CatalogPage]:
    """Объявления кабинета пачками — как их ждёт ядро. Сколько всего, Avito не говорит."""
    client = avito.get_client(account)
    for number in range(1, MAX_PAGES + 1):
        if number > 1:
            time.sleep(PAUSE)
        resources = client.items(page=number, per_page=avito.ITEMS_PER_PAGE)
        live = [item for item in resources if str(item.get("status") or "active") == "active"]
        cards = [made for made in (card(item) for item in live) if made]
        yield CatalogPage(items=cards, total=0, skipped=len(resources) - len(live))
        if len(resources) < avito.ITEMS_PER_PAGE:
            break
