"""Загрузка данных кабинета Яндекс Маркета: заказы в работе."""
from __future__ import annotations

import logging

from ...core import db
from ...core import sync as core_sync
from . import client as yandex
from . import store
from .client import YandexError

log = logging.getLogger("yandex.sync")


def sync_account(account: dict, *, returns_too: bool = True) -> dict:  # noqa: ARG001 - возвратов у Маркета в панели нет
    return sync_yandex(account)


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
