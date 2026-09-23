"""Загрузка данных кабинета Яндекс Маркета: заказы в работе."""
from __future__ import annotations

import logging

from ...core import catalog as core_catalog
from ...core import db
from ...core import sync as core_sync
from . import catalog
from . import client as yandex
from . import store
from .client import YandexError

log = logging.getLogger("yandex.sync")


def sync_account(account: dict, *, returns_too: bool = True) -> dict:  # noqa: ARG001 - возвратов у Маркета в панели нет
    result = sync_yandex(account)
    result.update(sync_products(account))
    return result


def sync_yandex(account: dict | None = None) -> dict:
    """Заказы Яндекс Маркета, с которыми сборщику надо что-то сделать.

    Это статус PROCESSING на двух этапах: STARTED — подтверждён, можно
    собирать, и READY_TO_SHIP — собран и ждёт отгрузки. Остальное панель не
    запрашивает вовсе: заказ уже уехал или отменён, и лишняя строка на складе —
    лишняя ошибка.

    Фильтр уходит в запрос, но на него не полагаемся: всё, что пришло с другим
    этапом, отбрасывается на нашей стороне. Иначе достаточно одной перемены в
    API, чтобы сборщик увидел лишнее.
    """
    account = core_sync._account(account)
    if account is None:
        return {"yandex": 0}
    account_id = account["id"]
    client = yandex.get_client(account)

    raw_orders: list[dict] = []
    token: str | None = None
    for _page in range(core_sync.RETURNS_MAX_PAGES):
        try:
            page, token = client.orders(
                substatuses=list(yandex.WORK_SUBSTATUSES), page_token=token
            )
        except YandexError as exc:
            log.warning("Заказы Маркета недоступны: %s", exc)
            raise
        raw_orders.extend(page)
        if not token:
            break

    seen: set[str] = set()
    saved = 0
    skipped = 0
    keep = []
    for raw in raw_orders:
        if str(raw.get("substatus") or "") in yandex.WORK_SUBSTATUSES:
            keep.append(raw)
        else:
            skipped += 1
    if keep:
        with db.write() as conn:
            for raw in keep:
                seen.add(store.upsert_yandex_order(conn, account_id, raw))
                saved += 1

    # Заказ ушёл из рабочих этапов — Маркет его больше не отдаёт, убираем и мы.
    stale = [
        row["id"]
        for row in db.query("SELECT id FROM yandex_orders WHERE account_id = ?", (account_id,))
        if row["id"] not in seen
    ]
    if stale:
        placeholders = ",".join("?" for _ in stale)
        with db.write() as conn:
            conn.execute(
                f"DELETE FROM yandex_order_items WHERE account_id = ? AND order_id IN ({placeholders})",
                [account_id] + stale,
            )
            conn.execute(
                f"DELETE FROM yandex_orders WHERE account_id = ? AND id IN ({placeholders})",
                [account_id] + stale,
            )
    result = {"yandex": saved}
    if skipped:
        result["yandex_skipped"] = skipped
    if stale:
        result["yandex_gone"] = len(stale)
    return result


def sync_products(account: dict | None = None, limit: int = 500) -> dict:
    """Подтянуть карточки товаров из заказов: штрихкоды, название, картинку.

    Полный каталог перечитывает кнопка в разделе «Товары» — это минуты. Здесь
    только то, что встретилось в заказах и чего в каталоге ещё нет: сборщик
    должен найти товар по штрихкоду, не дожидаясь обхода всего кабинета.
    """
    account = core_sync._account(account)
    if account is None:
        return {"yandex_products": 0}
    account_id = account["id"]
    rows = db.query(
        """
        SELECT DISTINCT i.offer_id FROM yandex_order_items i
        LEFT JOIN products p ON p.sku = i.offer_id AND p.account_id = i.account_id
        WHERE i.account_id = ? AND i.offer_id IS NOT NULL AND i.offer_id != '' AND p.sku IS NULL
        LIMIT ?
        """,
        (account_id, limit),
    )
    offers = [row["offer_id"] for row in rows if row["offer_id"]]
    if not offers:
        return {"yandex_products": 0}

    client = yandex.get_client(account)
    total = 0
    for start in range(0, len(offers), yandex.CATALOG_PAGE_LIMIT):
        chunk = offers[start : start + yandex.CATALOG_PAGE_LIMIT]
        try:
            mappings, _token = client.offer_mappings(offer_ids=chunk)
        except YandexError as exc:
            log.warning("Карточки товаров Маркета недоступны: %s", exc)
            break
        cards = [card for card in (catalog.card(item) for item in mappings) if card]
        if cards:
            with db.write() as conn:
                total += core_catalog.save(conn, account_id, cards)
        # Артикул без карточки (товар в архиве или удалён из каталога) — заводим
        # пустую строку с названием из заказа, чтобы не спрашивать о нём каждую
        # минуту. Полный обход в «Товарах» заполнит её, когда товар вернётся.
        found = {card["sku"] for card in cards}
        missing = [offer for offer in chunk if offer not in found]
        if missing:
            with db.write() as conn:
                for offer in missing:
                    known = db.query_one(
                        "SELECT name FROM yandex_order_items WHERE account_id = ? AND offer_id = ? LIMIT 1",
                        (account_id, offer),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO products(account_id, sku, offer_id, name, barcodes, updated_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (account_id, offer, offer, (known["name"] if known else None), "[]", db.now_iso()),
                    )
    return {"yandex_products": total}
