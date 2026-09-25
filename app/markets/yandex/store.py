"""Заказы Яндекс Маркета: таблицы, разбор ответов, поля для шаблонов."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from ...core import db
from ...core import orders as core_orders
from ...core.catalog import offer_barcodes
from ...core.store import (_dt, _num, _text, claim_is_active, hours_left, local_time,
                           urgency as urgency_of)

SCHEMA = """
CREATE TABLE IF NOT EXISTS yandex_orders (
    account_id      INTEGER NOT NULL,
    -- orderId Маркета. Храним строкой: так же, как у других площадок.
    id              TEXT NOT NULL,
    -- Идентификатор магазина. Приходит в самом заказе, в настройках его не
    -- спрашивают — а нужен он для ярлыка одного заказа и сверки отгрузки.
    campaign_id     TEXT,
    external_id     TEXT,
    status          TEXT,
    substatus       TEXT,
    program_type    TEXT,
    delivery_type   TEXT,
    service_name    TEXT,
    -- Дата отгрузки в службу доставки: по ней считается срочность.
    shipment_date   TEXT,
    shipment_id     TEXT,
    buyer_type      TEXT,
    notes           TEXT,
    total           REAL,
    items_count     INTEGER DEFAULT 0,
    positions_count INTEGER DEFAULT 0,
    created_at_api  TEXT,
    updated_at_api  TEXT,
    raw             TEXT,
    local_state     TEXT NOT NULL DEFAULT 'new',
    packed_at       TEXT,
    packed_by       TEXT,
    claim_user_id   INTEGER,
    claim_login     TEXT,
    claim_at        TEXT,
    printed_at      TEXT,
    print_count     INTEGER NOT NULL DEFAULT 0,
    -- Отметка о выгрузке ярлыка на компьютер — см. postings.label_saved_at.
    label_saved_at  TEXT,
    first_seen_at   TEXT,
    updated_at      TEXT,
    PRIMARY KEY (account_id, id)
);
CREATE INDEX IF NOT EXISTS idx_yandex_sub ON yandex_orders(account_id, substatus, local_state);

CREATE TABLE IF NOT EXISTS yandex_order_items (
    account_id INTEGER NOT NULL,
    order_id   TEXT NOT NULL,
    -- id позиции внутри заказа: у одного артикула бывает несколько строк.
    item_id    TEXT NOT NULL,
    offer_id   TEXT,
    name       TEXT,
    quantity   INTEGER NOT NULL DEFAULT 1,
    price      REAL,
    PRIMARY KEY (account_id, order_id, item_id)
);

-- Отчёт об отгруженных товарах. Строка появляется в момент, когда сошлась пара
-- «штрихкод товара -> номер отправления», а не при открытии заказа.
"""


def _yandex_dt(value: Any) -> str | None:
    """Дата Маркета к ISO-8601 UTC.

    Маркет пишет даты по-своему: «ДД-ММ-ГГГГ ЧЧ:ММ:СС» для моментов и
    «ДД-ММ-ГГГГ» для дня отгрузки, время московское и без смещения. Хранить это
    как есть нельзя: срочность считается от момента в UTC, а строки такого вида
    даже не сортируются. День без времени берём как его конец — отгрузить надо
    в этот день, и до полуночи заказ не просрочен.
    """
    raw = _text(value)
    if not raw:
        return None
    for fmt, whole_day in (("%d-%m-%Y %H:%M:%S", False), ("%d-%m-%Y", True)):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if whole_day:
            parsed = parsed.replace(hour=23, minute=59, second=59)
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=3)))
        return parsed.astimezone(timezone.utc).isoformat()
    return _dt(raw)


# --------------------------------------------------------------- Яндекс Маркет
def upsert_yandex_order(conn: sqlite3.Connection, account_id: int, raw: dict) -> str:
    """Заказ из POST /v1/businesses/{businessId}/orders, не трогая наши отметки.

    Локальное состояние сборки (packed_at, кто взял в работу, выгружен ли ярлык)
    принадлежит панели: площадка о нём не знает и затирать его нельзя.
    """
    order_id = _text(raw.get("orderId"))
    if not order_id:
        raise ValueError("В ответе Маркета нет orderId")

    delivery = raw.get("delivery") or {}
    shipment = delivery.get("shipment") or {}
    prices = raw.get("prices") or {}
    payment = prices.get("payment") or {}
    items = raw.get("items") or []
    quantity = sum(int(item.get("count") or 0) for item in items)

    now = db.now_iso()
    existing = conn.execute(
        "SELECT first_seen_at FROM yandex_orders WHERE account_id = ? AND id = ?", (account_id, order_id)
    ).fetchone()

    conn.execute(
        """
        INSERT INTO yandex_orders (
            account_id, id, campaign_id, external_id, status, substatus, program_type,
            delivery_type, service_name, shipment_date, shipment_id, buyer_type, notes,
            total, items_count, positions_count, created_at_api, updated_at_api, raw,
            first_seen_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(account_id, id) DO UPDATE SET
            campaign_id = excluded.campaign_id, external_id = excluded.external_id,
            status = excluded.status, substatus = excluded.substatus,
            program_type = excluded.program_type, delivery_type = excluded.delivery_type,
            service_name = excluded.service_name, shipment_date = excluded.shipment_date,
            shipment_id = excluded.shipment_id, buyer_type = excluded.buyer_type,
            notes = excluded.notes, total = excluded.total, items_count = excluded.items_count,
            positions_count = excluded.positions_count, created_at_api = excluded.created_at_api,
            updated_at_api = excluded.updated_at_api, raw = excluded.raw,
            updated_at = excluded.updated_at
        """,
        (
            account_id,
            order_id,
            _text(raw.get("campaignId")),
            _text(raw.get("externalOrderId")),
            _text(raw.get("status")),
            _text(raw.get("substatus")),
            _text(raw.get("programType")),
            _text(delivery.get("type")),
            _text(delivery.get("serviceName")),
            _yandex_dt(shipment.get("shipmentDate")),
            _text(shipment.get("id")),
            _text(raw.get("buyerType")),
            _text(raw.get("notes")),
            _num(payment.get("value")),
            quantity,
            len(items),
            _yandex_dt(raw.get("creationDate")),
            _yandex_dt(raw.get("updateDate")),
            json.dumps(raw, ensure_ascii=False),
            (existing["first_seen_at"] if existing else now) or now,
            now,
        ),
    )

    # Состав перечитываем целиком: позицию из заказа могут убрать, и остаться
    # в панели она не должна — сборщик пойдёт искать то, чего в заказе нет.
    conn.execute(
        "DELETE FROM yandex_order_items WHERE account_id = ? AND order_id = ?", (account_id, order_id)
    )
    for item in items:
        item_id = _text(item.get("id")) or _text(item.get("offerId"))
        if not item_id:
            continue
        item_prices = item.get("prices") or {}
        conn.execute(
            """
            INSERT INTO yandex_order_items(account_id, order_id, item_id, offer_id, name, quantity, price)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(account_id, order_id, item_id) DO UPDATE SET
                offer_id = excluded.offer_id, name = excluded.name,
                quantity = excluded.quantity, price = excluded.price
            """,
            (
                account_id,
                order_id,
                item_id,
                _text(item.get("offerId")),
                _text(item.get("offerName")),
                int(item.get("count") or 1),
                _num((item_prices.get("payment") or {}).get("value")),
            ),
        )
    return order_id


def yandex_items(account_id: int, order_id: str) -> list[dict]:
    """Состав заказа вместе со штрихкодами из каталога панели.

    Маркет штрихкодов не отдаёт — сверять скан не с чем. Зато артикул (offerId)
    у товара тот же, что на других площадках, и по нему в каталоге находятся
    штрихкоды: тогда сборщик сканирует товар как обычно, а пересорт ловится.
    """
    rows = db.query(
        "SELECT * FROM yandex_order_items WHERE account_id = ? AND order_id = ? ORDER BY name",
        (account_id, order_id),
    )
    items = []
    for row in rows:
        item = dict(row)
        item["barcodes"] = offer_barcodes(item.get("offer_id"))
        items.append(item)
    return items


def yandex_view(row: sqlite3.Row | dict, *, with_items: bool = True) -> dict:
    """Строка БД -> объект для шаблона: подписи, срочность, состав."""
    from .client import DELIVERY_LABELS, SUBSTATUS_LABELS

    data = dict(row)
    data.pop("raw", None)
    substatus = data.get("substatus") or ""
    data["status_label"] = SUBSTATUS_LABELS.get(substatus, substatus or data.get("status") or "")
    data["delivery_label"] = DELIVERY_LABELS.get(
        data.get("delivery_type") or "", data.get("delivery_type") or ""
    )
    # Срочность считаем по дате отгрузки: именно её нельзя пропустить.
    deadline = data.get("shipment_date")
    left = hours_left(deadline)
    data["deadline"] = deadline
    data["deadline_local"] = local_time(deadline, "%d.%m") if deadline else ""
    data["hours_left"] = round(left, 1) if left is not None else None
    data["urgency"] = urgency_of(deadline)
    data["created_local"] = local_time(data.get("created_at_api"))
    data["packed_at_local"] = local_time(data.get("packed_at"))
    data["claim_active"] = claim_is_active(data.get("claim_at"))
    if with_items:
        data["items"] = yandex_items(data["account_id"], data["id"])
    return data


# ------------------------------------------------ раздел «Заказы»
# Статусы склада. Маркет о нашей сборке не знает: собранный в панели заказ —
# «Собран», в каком бы подстатусе Маркета он ни был.
BOARD_STATUS_SQL = """CASE
    WHEN o.local_state = 'packed' THEN 'packed'
    WHEN o.substatus = 'STARTED' THEN 'packaging'
    WHEN o.substatus = 'READY_TO_SHIP' THEN 'deliver'
END"""
BOARD_SEARCH_SQL = """(o.id LIKE ? OR o.external_id LIKE ? OR o.service_name LIKE ?
    OR EXISTS (SELECT 1 FROM yandex_order_items i
               WHERE i.account_id = o.account_id AND i.order_id = o.id
               AND (i.name LIKE ? OR i.offer_id LIKE ?)))"""


def board_card(row) -> dict:
    """Заказ Маркета -> строка раздела «Заказы»."""
    order = yandex_view(row)
    tags = [("", f"{order.get('positions_count') or 0} поз. · {order.get('items_count') or 0} шт")]
    return {
        "id": order["id"],
        "number": order["id"],
        "sub": order.get("external_id") or "",
        "tags": tags,
        "deadline": order.get("deadline"),
        "deadline_local": order.get("deadline_local"),
        "urgency": order.get("urgency"),
        # Штрихкода нет в каталоге ни по одному кабинету — сканировать нечем.
        "items": [{"quantity": item["quantity"], "name": item.get("name") or "Без названия",
                   "code": item.get("offer_id") or item.get("item_id"),
                   "warn": "" if item.get("barcodes") else "нет ШК"}
                  for item in order.get("items") or []],
        "image": None,
        "delivery": [order.get("delivery_label"), order.get("service_name"), order.get("notes")],
        "own_status": "",
        "note": "",
        "printed": order.get("print_count") or 0,
        "packed_by": order.get("packed_by"),
        "packed_at": order.get("packed_at"),
        "packed_at_local": order.get("packed_at_local"),
        "claim": order.get("claim_login") if order.get("claim_active") else "",
        # Ярлык у Маркета есть с подтверждения заказа — в любом из статусов.
        "label": True,
        "actions": [],
    }


# ------------------------------------------------ общий список на рабочем месте
FEED_SQL = """
SELECT o.account_id, o.id, o.substatus, o.local_state, o.shipment_date, o.items_count,
       {goods}
  FROM yandex_orders o
 WHERE o.account_id IN ({marks}) AND o.substatus IN (?, ?)
 ORDER BY (o.shipment_date IS NULL), o.shipment_date
 LIMIT ?
"""


def orders_feed(account_ids: list[int], limit: int = 300) -> list[dict]:
    """Заказы кабинетов Маркета для общего списка на «Сборке»."""
    if not account_ids:
        return []
    from .client import SUBSTATUS_LABELS, SUBSTATUS_READY_TO_SHIP, SUBSTATUS_STARTED

    rows = db.query(
        FEED_SQL.format(
            goods=core_orders.goods_column("yandex_order_items", on="i.order_id = o.id"),
            marks=core_orders.marks(account_ids),
        ),
        list(account_ids) + [SUBSTATUS_STARTED, SUBSTATUS_READY_TO_SHIP, limit],
    )
    feed = []
    for row in rows:
        substatus = row["substatus"] or ""
        packed = (row["local_state"] or "new") == "packed"
        feed.append(core_orders.row(
            row["account_id"], row["id"],
            goods=row["goods"], quantity=row["items_count"], deadline=row["shipment_date"],
            status_label="Собран" if packed else SUBSTATUS_LABELS.get(substatus, substatus),
            in_work=not packed,
        ))
    return feed
