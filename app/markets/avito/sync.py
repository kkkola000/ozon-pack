"""Загрузка данных кабинета Avito: заказы в рабочих статусах и возвраты к выдаче."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from ...core import db
from ...core import sync as core_sync
from ...core.config import settings
from . import client as avito
from . import returns, store
from .client import AvitoError

log = logging.getLogger("avito.sync")


def sync_account(account: dict, *, returns_too: bool = True) -> dict:  # noqa: ARG001 - возвраты приходят с заказами
    return sync_avito(account)


def sync_avito(account: dict | None = None) -> dict:
    """Заказы Avito, с которыми сборщику надо что-то сделать.

    Это «ожидает подтверждения», «ждёт отправки» и возвраты. Из возвратов
    сохраняются только те, что уже лежат в пункте выдачи (returnStatus =
    ready_to_pickup): пока посылка едет обратно, забирать нечего, и в панели
    ей делать нечего. Остальные статусы заказа не запрашиваются вовсе —
    уехавший в «в пути» или «доставлен» заказ из панели просто исчезает.
    """
    account = core_sync._account(account)
    if account is None:
        return {"avito": 0}
    account_id = account["id"]
    client = avito.get_client(account)
    date_from = datetime.now(timezone.utc) - timedelta(days=settings.avito_days_back)

    seen: set[str] = set()
    saved = 0
    # Что именно вернул Avito по возвратам — видно на странице возвратов.
    returns_seen: dict[str, int] = {}
    try:
        # Два запроса, а не один. dateFrom у Avito отсекает по дате СОЗДАНИЯ
        # заказа: покупку сделали два месяца назад, вернули сегодня — с окном
        # в 30 дней такой возврат в панель бы не попал. Плюс возвраты не
        # должны конкурировать с текущими заказами за страницы выдачи.
        orders = client.orders_all(statuses=list(avito.WORK_STATUSES), date_from=date_from)
        orders += client.orders_all(statuses=list(avito.RETURN_STATUSES))
    except AvitoError as exc:
        log.warning("Заказы Avito недоступны: %s", exc)
        raise

    keep = []
    for raw in orders:
        if raw.get("status") != avito.STATUS_ON_RETURN:
            keep.append(raw)
            continue
        return_status = ((raw.get("returnPolicy") or {}).get("returnStatus")) or "—"
        returns_seen[return_status] = returns_seen.get(return_status, 0) + 1
        # Забрать можно только то, что доехало до пункта выдачи.
        if avito.is_ready_for_pickup(return_status):
            keep.append(raw)
            if not avito.pickup_address(raw):
                # Адрес ПВЗ Avito отдаёт не всегда — видно, где его искать дальше.
                log.info(
                    "Возврат %s без адреса ПВЗ; delivery: %s, returnPolicy: %s",
                    raw.get("marketplaceId") or raw.get("id"),
                    sorted((raw.get("delivery") or {}).keys()),
                    sorted((raw.get("returnPolicy") or {}).keys()),
                )

    if keep:
        with db.write() as conn:
            for raw in keep:
                seen.add(store.upsert_avito_order(conn, account_id, raw))
                saved += 1

    # Заказ ушёл из рабочих статусов — Avito его больше не отдаёт.
    #
    # Возврат при этом не пропажа, а факт: пропал из пункта выдачи — значит,
    # его забрали. Своего статуса «получен» у Avito нет, и это единственный
    # признак получения. Такую строку не удаляем, а помечаем полученной: из
    # полученных за день составляется акт, как у Ozon по статусу «Получен».
    # Раньше она просто исчезала, и акта по возвратам Avito не выходило вовсе.
    gone = [
        dict(row)
        for row in db.query(
            "SELECT id, status, received_at, act_id FROM avito_orders WHERE account_id = ?", (account_id,)
        )
        if row["id"] not in seen
    ]
    received_now = [
        row["id"] for row in gone
        if row["status"] == avito.STATUS_ON_RETURN and not row["received_at"]
    ]
    # Полученный возврат и дальше держим в базе: по нему ещё нужна отметка, и
    # он лежит в акте. Удаляем только то, к работе отношения не имеющее.
    stale = [
        row["id"] for row in gone
        if row["id"] not in set(received_now) and not row["received_at"] and not row["act_id"]
    ]
    received_count = 0
    if received_now or stale:
        with db.write() as conn:
            if received_now:
                received_count = returns.mark_received(conn, account_id, received_now)
            if stale:
                placeholders = ",".join("?" for _ in stale)
                conn.execute(
                    f"DELETE FROM avito_order_items WHERE account_id = ? AND order_id IN ({placeholders})",
                    [account_id] + stale,
                )
                conn.execute(
                    f"DELETE FROM avito_orders WHERE account_id = ? AND id IN ({placeholders})",
                    [account_id] + stale,
                )
    result = {"avito": saved}
    ready = db.query_one(
        "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND status = ? AND received_at IS NULL",
        (account_id, avito.STATUS_ON_RETURN),
    )["c"]
    if ready:
        result["avito_returns"] = ready
    skipped = sum(count for code, count in returns_seen.items() if not avito.is_ready_for_pickup(code))
    if skipped:
        result["avito_returns_skipped"] = skipped
    if received_count:
        result["avito_received"] = received_count
    if stale:
        result["avito_gone"] = len(stale)
    return result
