"""Сопоставленные карточки на сборке: чем одна карточка помогает другой.

Сопоставление (core/product_links.py) говорит: эти карточки разных кабинетов —
одна коробка на полке. Для сборки из этого следуют три вещи:

* **штрихкод** — годится любой из группы. Наклейка на коробке одна, и какая
  площадка её завела, сборщику всё равно: штрихкод карточки Маркета подходит к
  заказу Ozon, если карточки сопоставлены;
* **фото** — своё, а нет своего — основной карточки группы или любой другой.
  У Avito фото в каталоге нет совсем, у Маркета оно бывает не у всех;
* **набор** — состав, заданный одной карточке, действует на всю группу
  (core/product_sets.py): коробку собирают из тех же частей, из какого бы
  магазина ни пришёл заказ.

Здесь только чтение: кто с кем сопоставлен, решают в разделе «Товары».
Площадок по именам здесь нет — карточка для ядра просто пара (кабинет, SKU).
"""
from __future__ import annotations

from collections.abc import Iterable

from . import db

Card = tuple[int, str]


def card(account_id: int, sku: str) -> Card:
    return int(account_id), str(sku)


def others(account_id: int, sku: str) -> list[dict]:
    """Сопоставленные с карточкой, кроме неё самой: основная первой.

    Нет группы — пустой список. Порядок тот же, что в разделе «Товары»: фото
    и штрихкоды берутся в первую очередь у основной карточки.
    """
    rows = db.query(
        """
        SELECT p.account_id, p.sku, p.offer_id, p.name, p.image, p.barcodes, l2.is_main,
               a.title AS shop, a.marketplace AS market
          FROM product_links l1
          JOIN product_links l2 ON l2.group_id = l1.group_id
               AND NOT (l2.account_id = l1.account_id AND l2.sku = l1.sku)
          JOIN products p ON p.account_id = l2.account_id AND p.sku = l2.sku
          JOIN accounts a ON a.id = p.account_id
         WHERE l1.account_id = ? AND l1.sku = ?
         ORDER BY l2.is_main DESC, a.sort, a.id, p.sku
        """,
        card(account_id, sku),
    )
    return [dict(row) for row in rows]


def extras(account_id: int, sku: str | None, *, image: str | None = None,
           barcodes: Iterable[str] = ()) -> tuple[list[str], str | None]:
    """Штрихкоды и фото позиции с учётом сопоставленных карточек.

    image и barcodes — то, что у позиции есть своё: свои штрихкоды идут первыми,
    своё фото сильнее чужого. Позиция без карточки (sku пустой) остаётся как
    есть.
    """
    own = [str(code) for code in barcodes if str(code or "").strip()]
    if not sku:
        return own, image or None
    linked = others(account_id, sku)
    codes = list(dict.fromkeys(own + [str(code) for row in linked for code in db.json_list(row["barcodes"])
                                      if str(code or "").strip()]))
    photo = image or next((row["image"] for row in linked if row["image"]), None)
    return codes, photo


def group(cards: Iterable[Card]) -> set[Card]:
    """Карточки вместе со всем, с чем они сопоставлены."""
    found: set[Card] = set()
    for account_id, sku in cards:
        found.add(card(account_id, sku))
        rows = db.query(
            "SELECT l2.account_id, l2.sku FROM product_links l1 "
            "JOIN product_links l2 ON l2.group_id = l1.group_id "
            "WHERE l1.account_id = ? AND l1.sku = ?",
            card(account_id, sku),
        )
        found.update(card(row["account_id"], row["sku"]) for row in rows)
    return found


def owners(codes: Iterable[str]) -> set[Card]:
    """Карточки любых кабинетов, у которых есть такой штрихкод."""
    wanted = [str(code) for code in codes if str(code or "").strip()]
    if not wanted:
        return set()
    marks = ",".join("?" for _ in wanted)
    rows = db.query(f"SELECT account_id, sku FROM product_barcodes WHERE barcode IN ({marks})", wanted)
    return {card(row["account_id"], row["sku"]) for row in rows}


def cards_of_code(codes: Iterable[str]) -> set[Card]:
    """Что мог означать скан: чей это штрихкод и всё, с чем эти карточки сопоставлены."""
    return group(owners(codes))


def skus_here(account_id: int, codes: Iterable[str]) -> list[str]:
    """SKU этого кабинета, к которым подходит скан — своим штрихкодом или сопоставленной карточки."""
    account_id = int(account_id)
    return sorted(sku for owner, sku in cards_of_code(codes) if owner == account_id)


def photo(cards: Iterable[Card]) -> str | None:
    """Первое фото среди карточек, а нет ни одного — среди сопоставленных с ними."""
    ordered = sorted(set(cards))
    for account_id, sku in ordered:
        row = db.query_one("SELECT image FROM products WHERE account_id = ? AND sku = ?", (account_id, sku))
        if row and row["image"]:
            return row["image"]
    for account_id, sku in ordered:
        image = extras(account_id, sku)[1]
        if image:
            return image
    return None


def codes_of(cards: Iterable[Card]) -> set[str]:
    """Все штрихкоды этих карточек — чтобы узнать часть набора по коду соседа."""
    found: set[str] = set()
    for account_id, sku in cards:
        rows = db.query(
            "SELECT barcode FROM product_barcodes WHERE account_id = ? AND sku = ?", card(account_id, sku)
        )
        found.update(row["barcode"] for row in rows)
    return found
