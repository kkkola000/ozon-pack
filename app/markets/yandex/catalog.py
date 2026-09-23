"""Каталог товаров Яндекс Маркета: один метод, страницы по токену.

POST /v2/businesses/{businessId}/offer-mappings отдаёт карточку целиком —
артикул продавца, название, картинки и штрихкоды. Второго запроса, как у Ozon,
не нужно: обход в один проход по страницам.

Идентификатором товара в панели берём offerId — артикул продавца. У Маркета
есть и свой marketSku, но он появляется только после привязки к карточке
Маркета, а сборке нужен ключ, который есть всегда и совпадает с тем, что стоит
в заказе.

Архив Маркет по умолчанию не отдаёт (фильтр `archived` мы не заполняем), но
проверку оставляем: архивный товар в каталоге панели — это набор, который
нельзя собрать.
"""
from __future__ import annotations

from collections.abc import Iterator

from ...core.store import _text
from ..base import CatalogPage
from . import client as yandex

# Предел числа страниц — на случай, если токен следующей страницы начнёт
# возвращаться по кругу: молчаливый бесконечный обход хуже оборванного.
MAX_PAGES = 500


def _picture(offer: dict) -> str | None:
    """Первая картинка товара: сначала прямые ссылки, потом загруженные файлы."""
    pictures = offer.get("pictures") or []
    if isinstance(pictures, list):
        for url in pictures:
            if isinstance(url, str) and url.strip():
                return url.strip()
    media = ((offer.get("mediaFiles") or {}).get("pictures")) or []
    for item in media if isinstance(media, list) else []:
        if isinstance(item, dict) and str(item.get("url") or "").strip():
            return str(item["url"]).strip()
    return None


def card(mapping: dict) -> dict | None:
    """Карточка Маркета в общем виде каталога панели. Без артикула — не карточка."""
    offer = mapping.get("offer") or {}
    sku = str(offer.get("offerId") or "").strip()
    if not sku:
        return None
    barcodes = [str(code).strip() for code in (offer.get("barcodes") or []) if str(code).strip()]
    return {
        "sku": sku,
        # У Маркета артикул продавца и есть ключ товара: по нему сборщик находит
        # позицию заказа, поэтому в обеих колонках он же.
        "offer_id": sku,
        "name": _text(offer.get("name")),
        "image": _picture(offer),
        "barcodes": barcodes,
    }


def is_archived(mapping: dict) -> bool:
    return bool((mapping.get("offer") or {}).get("archived"))


def pages(account: dict) -> Iterator[CatalogPage]:
    """Каталог кабинета страницами — как его ждёт ядро.

    Сколько всего товаров, Маркет не сообщает: страницы идут по токену, и
    total остаётся нулём — кнопка тогда считает пройденные карточки.
    """
    client = yandex.get_client(account)
    token: str | None = None
    for _page in range(MAX_PAGES):
        mappings, token = client.offer_mappings(page_token=token)
        if not mappings:
            break
        fresh = [item for item in mappings if not is_archived(item)]
        cards = [item for item in (card(mapping) for mapping in fresh) if item]
        yield CatalogPage(items=cards, skipped=len(mappings) - len(fresh))
        if not token:
            break
