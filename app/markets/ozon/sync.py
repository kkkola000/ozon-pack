"""Загрузка данных кабинета Ozon: отправления и карточки товаров.

Возвраты грузит returns.sync_returns — у них своё расписание.
"""
from __future__ import annotations

import logging

from ...core import db
from ...core import sync as core_sync
from ...core.config import settings
from . import client as ozon
from . import returns, store
from .client import OzonError

log = logging.getLogger("ozon.sync")


def sync_account(account: dict, *, returns_too: bool = True) -> dict:
    """Один проход по кабинету. Возвраты — по отдельному расписанию, поэтому флагом."""
    result: dict = {}
    result.update(sync_postings(account))
    result.update(sync_products(account))
    if returns_too:
        result.update(returns.sync_returns(account))
    return result


def sync_postings(account: dict | None = None) -> dict:
    """Забрать отправления в рабочих статусах и освежить те, что из них ушли."""
    account = core_sync._account(account)
    if account is None:
        return {"saved": 0, "refreshed": 0}
    account_id = account["id"]
    client = ozon.get_client(account)
    since, to = core_sync._iso_window(settings.sync_days_back, settings.sync_days_forward)
    seen: set[str] = set()
    saved = 0

    for status in store.WORK_STATUSES:
        offset = 0
        while True:
            postings, has_next = client.posting_list(status, since, to, limit=core_sync.PAGE_LIMIT, offset=offset)
            if postings:
                with db.write() as conn:
                    for raw in postings:
                        store.upsert_posting(conn, account_id, raw)
                        seen.add(raw["posting_number"])
                        saved += 1
            if not has_next or not postings:
                break
            offset += core_sync.PAGE_LIMIT

    # Отправление могло уехать в «Доставляется» или отмениться — узнаём точный статус.
    stale = db.query(
        "SELECT posting_number FROM postings WHERE account_id = ? AND status IN (?, ?)",
        (account_id, *store.WORK_STATUSES),
    )
    refreshed = 0
    for row in stale:
        number = row["posting_number"]
        if number in seen:
            continue
        try:
            raw = client.posting_get(number)
        except OzonError as exc:
            log.warning("Не удалось обновить %s: %s", number, exc)
            continue
        if raw:
            with db.write() as conn:
                store.upsert_posting(conn, account_id, raw)
            refreshed += 1
        else:
            db.execute(
                "UPDATE postings SET status = 'unknown', updated_at = ? WHERE account_id = ? AND posting_number = ?",
                (db.now_iso(), account_id, number),
            )
    return {"saved": saved, "refreshed": refreshed}


def sync_products(account: dict | None = None, limit: int = 500) -> dict:
    """Подтянуть карточки товаров (штрихкоды и фото) для новых SKU."""
    account = core_sync._account(account)
    if account is None:
        return {"products": 0}
    account_id = account["id"]
    rows = db.query(
        """
        SELECT DISTINCT i.sku FROM posting_items i
        LEFT JOIN products p ON p.sku = i.sku AND p.account_id = i.account_id
        WHERE i.account_id = ? AND p.sku IS NULL
        UNION
        SELECT DISTINCT r.sku FROM returns r
        LEFT JOIN products p2 ON p2.sku = r.sku AND p2.account_id = r.account_id
        WHERE r.account_id = ? AND p2.sku IS NULL AND r.sku IS NOT NULL
        LIMIT ?
        """,
        (account_id, account_id, limit),
    )
    skus = [row["sku"] for row in rows if row["sku"]]
    if not skus:
        return {"products": 0}

    client = ozon.get_client(account)
    total = 0
    for start in range(0, len(skus), 100):
        chunk = skus[start : start + 100]
        try:
            items = client.product_info(skus=chunk)
        except OzonError as exc:
            log.warning("Карточки товаров недоступны: %s", exc)
            break
        if items:
            with db.write() as conn:
                total += store.upsert_products(conn, account_id, items)
        # SKU без карточки (например, товар архивирован) — чтобы не спрашивать бесконечно.
        found = {str(item.get("sku") or item.get("id")) for item in items}
        missing = [sku for sku in chunk if sku not in found]
        if missing:
            with db.write() as conn:
                for sku in missing:
                    name = db.query_one(
                        "SELECT name, offer_id FROM posting_items WHERE account_id = ? AND sku = ? LIMIT 1",
                        (account_id, sku),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO products(account_id, sku, offer_id, name, barcodes, updated_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (account_id, sku, (name["offer_id"] if name else None),
                         (name["name"] if name else None), "[]", db.now_iso()),
                    )
    return {"products": total}
