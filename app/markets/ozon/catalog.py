"""Обход каталога Ozon: список артикулов, потом карточки пачками.

Ozon отдаёт каталог в два приёма. Сначала /v3/product/list — страницами по
last_id, в них только артикул и признак архива. Потом /v3/product/info/list —
карточки с названием, фото и штрихкодами, до ста артикулов за запрос. Одним
методом это не получить, поэтому обход двухэтапный, и ядру про это знать
незачем: оно получает готовые пачки карточек.

Карточку приводим к общему виду здесь же: в каталоге панели лежат товары трёх
площадок, и разбирать ответ Ozon за пределами его пакета нечему.
"""
from __future__ import annotations

from collections.abc import Iterator

from ...core.store import _text
from ..base import CatalogPage
from . import client as ozon

# Страницами по столько Ozon отдаёт список артикулов. Предел числа страниц —
# на всякий случай: без него ошибка в курсоре крутила бы обход бесконечно.
PAGE_LIMIT = 1000
MAX_PAGES = 200
# Столько артикулов за раз принимает /v3/product/info/list.
INFO_CHUNK = 100


def is_archived(item: dict) -> bool:
    """Архивный ли товар. Имя поля у Ozon в разных методах разное.

    Неизвестное значение считаем «не архив»: спрятать живой товар хуже, чем
    показать архивный — из-за первого набор не соберёшь.
    """
    for key in ("archived", "is_archived"):
        value = item.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes"):
            return True
    return False


def card(item: dict) -> dict:
    """Карточка Ozon в общем виде каталога панели."""
    primary = item.get("primary_image") or item.get("images") or []
    if isinstance(primary, list):
        image = primary[0] if primary else None
    else:
        image = primary if isinstance(primary, str) else None
    return {
        "sku": str(item.get("sku") or item.get("id") or ""),
        "offer_id": _text(item.get("offer_id")),
        "name": _text(item.get("name")),
        "image": _text(image),
        "barcodes": list(item.get("barcodes") or []),
    }


def offers_of(client, *, on_page=None) -> tuple[list[str], int]:
    """Артикулы живых товаров кабинета и сколько отсеяно архивных."""
    offers: list[str] = []
    archived = 0
    last_id = ""
    for _page in range(MAX_PAGES):
        items, last_id, _total = client.product_list(limit=PAGE_LIMIT, last_id=last_id)
        if not items:
            break
        for item in items:
            if is_archived(item):
                archived += 1
                continue
            offer = str(item.get("offer_id") or "").strip()
            if offer:
                offers.append(offer)
        if on_page:
            on_page(len(offers))
        if not last_id:
            break
    return offers, archived


def pages(account: dict) -> Iterator[CatalogPage]:
    """Каталог кабинета пачками карточек — как его ждёт ядро.

    Пока идёт первый этап, отдаём пустые пачки с растущим total: человек у
    кнопки видит, что обход не встал, хотя карточек ещё нет ни одной.
    """
    client = ozon.get_client(account)
    found: list[int] = [0]
    listed: list[CatalogPage] = []

    def on_page(count: int) -> None:
        found[0] = count
        listed.append(CatalogPage(items=[], total=count))

    offers, archived = offers_of(client, on_page=on_page)
    yield from listed
    total = len(offers)
    # Архив, найденный на первом этапе, отдаём отдельной пустой пачкой: так он
    # доходит до итога и когда живых товаров не осталось вовсе.
    yield CatalogPage(items=[], total=total, skipped=archived)
    for start in range(0, total, INFO_CHUNK):
        chunk = offers[start : start + INFO_CHUNK]
        items = client.product_info(offer_ids=chunk)
        fresh = [item for item in items if not is_archived(item)]
        yield CatalogPage(
            items=[card(item) for item in fresh],
            total=total,
            # Товар мог уехать в архив между двумя запросами — считаем и такой.
            skipped=len(items) - len(fresh),
        )
