"""Заказы Avito: таблицы, разбор ответов, поля для шаблонов.

Возврат у Avito — состояние заказа (return_status), отдельной таблицы нет.
"""
from __future__ import annotations

import json
import sqlite3

from ...core import db
from ...core.store import (_dt, _num, _raw_json, _text, _with_mark, hours_left, local_time,
                           urgency as urgency_of)

SCHEMA = """
CREATE TABLE IF NOT EXISTS avito_orders (
    account_id      INTEGER NOT NULL,
    id              TEXT NOT NULL,
    marketplace_id  TEXT,
    status          TEXT,
    service_type    TEXT,
    service_name    TEXT,
    dispatch_number TEXT,
    tracking_number TEXT,
    terminal_code   TEXT,
    terminal_address TEXT,
    -- Имя покупателя нужно на листе возвратов, чтобы найти посылку в ПВЗ.
    -- Телефон панель не хранит: он нигде не показывается, а персональные
    -- данные без цели — лишний риск (см. _drop_buyer_contacts ниже).
    buyer_name      TEXT,
    confirm_till    TEXT,
    ship_till       TEXT,
    delivery_date   TEXT,
    return_status   TEXT,
    return_tracking TEXT,
    price           REAL,
    total           REAL,
    delivery_price  REAL,
    commission      REAL,
    items_count     INTEGER DEFAULT 0,
    positions_count INTEGER DEFAULT 0,
    actions         TEXT,
    created_at_api  TEXT,
    updated_at_api  TEXT,
    raw             TEXT,
    local_state     TEXT NOT NULL DEFAULT 'new',
    confirmed_at    TEXT,
    confirmed_by    TEXT,
    shipped_at      TEXT,
    shipped_by      TEXT,
    printed_at      TEXT,
    print_count     INTEGER NOT NULL DEFAULT 0,
    -- Отметка о выгрузке стикера на компьютер — см. такую же колонку в postings.
    label_saved_at  TEXT,
    -- Сборка на складе: у Avito нет штрихкодов товаров, поэтому отметка
    -- «собран» ставится по факту сканирования стикера и товара, а не площадкой.
    packed_at       TEXT,
    packed_by       TEXT,
    claim_user_id   INTEGER,
    claim_login     TEXT,
    claim_at        TEXT,
    -- Отметка сборщика при получении возврата — см. такие же колонки в returns.
    mark            TEXT,
    note            TEXT,
    mark_at         TEXT,
    mark_by         TEXT,
    act_id          TEXT,
    -- Возврат пропал из пункта выдачи — значит, его забрали. Своего статуса
    -- «получен» у Avito нет, и это единственный признак получения: по нему
    -- возврат попадает в акт, как у Ozon по статусу «Получен». Пишется один
    -- раз: вторая запись означала бы второй акт на ту же работу.
    received_at     TEXT,
    -- Тот же момент местной датой: по ней акты собираются «за указанное число».
    received_day    TEXT,
    first_seen_at   TEXT,
    updated_at      TEXT,
    PRIMARY KEY (account_id, id)
);
CREATE INDEX IF NOT EXISTS idx_avito_status ON avito_orders(account_id, status);
CREATE INDEX IF NOT EXISTS idx_avito_return ON avito_orders(account_id, return_status);

CREATE TABLE IF NOT EXISTS avito_order_items (
    account_id INTEGER NOT NULL,
    order_id   TEXT NOT NULL,
    avito_id   TEXT NOT NULL,
    seller_id  TEXT,
    title      TEXT,
    quantity   INTEGER NOT NULL DEFAULT 1,
    price      REAL,
    image      TEXT,
    location   TEXT,
    PRIMARY KEY (account_id, order_id, avito_id)
);

-- Заказы Яндекс Маркета. Отдельная таблица, а не общая с Ozon: у Маркета своя
-- пара «статус + этап обработки», свой идентификатор заказа и нет номера
-- отправления — сводить это в одну таблицу значило бы держать половину колонок
-- пустыми и гадать, чьи они.
"""


def upsert_avito_order(conn: sqlite3.Connection, account_id: int, raw: dict) -> str:
    """Заказ из GET /order-management/1/orders, не затирая локальные отметки."""
    order_id = str(raw.get("id") or "")
    if not order_id:
        raise ValueError("В ответе Avito нет id заказа")

    from . import client as avito_client

    delivery = raw.get("delivery") or {}
    buyer = delivery.get("buyerInfo") or {}
    prices = raw.get("prices") or {}
    schedules = raw.get("schedules") or {}
    return_policy = raw.get("returnPolicy") or {}
    items = raw.get("items") or []
    actions = [a.get("name") for a in (raw.get("availableActions") or []) if a.get("name")]

    now = db.now_iso()
    existing = conn.execute(
        "SELECT first_seen_at FROM avito_orders WHERE account_id = ? AND id = ?", (account_id, order_id)
    ).fetchone()

    conn.execute(
        """
        INSERT INTO avito_orders (
            account_id, id, marketplace_id, status, service_type, service_name, dispatch_number, tracking_number,
            terminal_code, terminal_address, buyer_name, confirm_till, ship_till, delivery_date,
            return_status, return_tracking,
            price, total, delivery_price, commission, items_count, positions_count, actions,
            created_at_api, updated_at_api, raw, first_seen_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(account_id, id) DO UPDATE SET
            marketplace_id = excluded.marketplace_id, status = excluded.status,
            service_type = excluded.service_type, service_name = excluded.service_name,
            dispatch_number = excluded.dispatch_number, tracking_number = excluded.tracking_number,
            terminal_code = excluded.terminal_code, terminal_address = excluded.terminal_address,
            buyer_name = excluded.buyer_name,
            confirm_till = excluded.confirm_till, ship_till = excluded.ship_till,
            delivery_date = excluded.delivery_date, return_status = excluded.return_status,
            return_tracking = excluded.return_tracking, price = excluded.price, total = excluded.total,
            delivery_price = excluded.delivery_price, commission = excluded.commission,
            items_count = excluded.items_count, positions_count = excluded.positions_count,
            actions = excluded.actions, created_at_api = excluded.created_at_api,
            updated_at_api = excluded.updated_at_api, raw = excluded.raw, updated_at = excluded.updated_at
        """,
        (
            account_id,
            order_id,
            _text(raw.get("marketplaceId")) or order_id,
            _text(raw.get("status")),
            _text(delivery.get("serviceType")),
            _text(delivery.get("serviceName")),
            _text(delivery.get("dispatchNumber")),
            _text(delivery.get("trackingNumber")),
            _text(avito_client.pickup_code(raw)),
            _text(avito_client.pickup_address(raw)),
            _text(buyer.get("fullName")),
            _dt(schedules.get("confirmTill")),
            _dt(schedules.get("shipTill")),
            _dt(schedules.get("deliveryDate")) or _dt(schedules.get("deliveryDateMax")),
            _text(return_policy.get("returnStatus")),
            _text(return_policy.get("trackingNumber")),
            _num(prices.get("price")),
            _num(prices.get("total")),
            _num(prices.get("delivery")),
            _num(prices.get("commission")),
            sum(int(i.get("count") or 0) for i in items),
            len(items),
            json.dumps(actions, ensure_ascii=False),
            _dt(raw.get("createdAt")),
            _dt(raw.get("updatedAt")),
            _raw_json(raw),
            (existing["first_seen_at"] if existing else now) or now,
            now,
        ),
    )

    conn.execute("DELETE FROM avito_order_items WHERE account_id = ? AND order_id = ?", (account_id, order_id))
    for item in items:
        avito_id = str(item.get("avitoId") or "")
        if not avito_id:
            continue
        item_prices = item.get("prices") or {}
        conn.execute(
            """
            INSERT INTO avito_order_items(account_id, order_id, avito_id, seller_id, title, quantity, price,
                                          image, location)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account_id, order_id, avito_id) DO UPDATE SET
                seller_id = excluded.seller_id, title = excluded.title, quantity = excluded.quantity,
                price = excluded.price, image = excluded.image, location = excluded.location
            """,
            (
                account_id,
                order_id,
                avito_id,
                _text(item.get("id")),
                _text(item.get("title")),
                int(item.get("count") or 1),
                _num(item_prices.get("price")),
                _text(item.get("image")),
                _text(item.get("location")),
            ),
        )
    return order_id


def avito_items(account_id: int, order_id: str) -> list[dict]:
    rows = db.query(
        "SELECT * FROM avito_order_items WHERE account_id = ? AND order_id = ? ORDER BY title",
        (account_id, order_id),
    )
    return [dict(row) for row in rows]


def avito_view(row: sqlite3.Row | dict, *, with_items: bool = True) -> dict:
    """Строка БД -> объект для шаблона: подписи статуса, срочность, действия."""
    from .client import RETURN_STATUS_LABELS, SERVICE_LABELS, STATUS_LABELS

    data = dict(row)
    data.pop("raw", None)
    status = data.get("status") or ""
    data["status_label"] = STATUS_LABELS.get(status, status)
    return_status = data.get("return_status") or ""
    data["return_label"] = RETURN_STATUS_LABELS.get(return_status, return_status or "—")
    data["service_label"] = SERVICE_LABELS.get(data.get("service_type") or "", data.get("service_type") or "")
    try:
        data["actions"] = json.loads(data.get("actions") or "[]")
    except ValueError:
        data["actions"] = []
    # Срок берём тот, который сейчас поджимает: подтверждение или отправка.
    deadline = data.get("confirm_till") if status == "on_confirmation" else data.get("ship_till")
    left = hours_left(deadline)
    data["deadline"] = deadline
    data["deadline_local"] = local_time(deadline)
    data["hours_left"] = round(left, 1) if left is not None else None
    data["urgency"] = urgency_of(deadline)
    data["created_local"] = local_time(data.get("created_at_api"))
    # Кто и когда собрал — во вкладке «Собранные» это главное, что нужно знать:
    # площадка о сборке не знает, и спросить, кроме панели, негде.
    data["packed_at_local"] = local_time(data.get("packed_at"))
    if with_items:
        data["items"] = avito_items(data["account_id"], data["id"])
    return _with_mark(data)


# ------------------------------------------------ общий список на рабочем месте
# Те же колонки, что у других площадок: список на «Сборке» показывает заказы
# всех кабинетов рядом и о площадках ничего не знает.
FEED_SQL = """
SELECT o.account_id, o.id, o.marketplace_id, o.status, o.local_state, o.confirm_till, o.ship_till,
       o.items_count,
       (SELECT GROUP_CONCAT(CASE WHEN i.quantity > 1 THEN i.title || ' ×' || i.quantity ELSE i.title END, ' · ')
          FROM avito_order_items i
         WHERE i.account_id = o.account_id AND i.order_id = o.id) AS goods
  FROM avito_orders o
 WHERE o.account_id IN ({marks}) AND o.status IN (?, ?, ?)
 ORDER BY (COALESCE(o.confirm_till, o.ship_till) IS NULL), COALESCE(o.confirm_till, o.ship_till)
 LIMIT ?
"""


def orders_feed(account_ids: list[int], limit: int = 300) -> list[dict]:
    """Заказы кабинетов Avito для общего списка, вместе с возвратами."""
    if not account_ids:
        return []
    from .client import (STATUS_LABELS, STATUS_ON_CONFIRMATION, STATUS_ON_RETURN,
                         STATUS_READY_TO_SHIP)

    marks = ",".join("?" for _ in account_ids)
    rows = db.query(
        FEED_SQL.format(marks=marks),
        list(account_ids) + [STATUS_ON_CONFIRMATION, STATUS_READY_TO_SHIP, STATUS_ON_RETURN, limit],
    )
    feed = []
    for row in rows:
        status = row["status"] or ""
        packed = (row["local_state"] or "new") == "packed"
        # Срок берём тот, который сейчас поджимает, — как на странице заказов.
        deadline = row["confirm_till"] if status == STATUS_ON_CONFIRMATION else row["ship_till"]
        feed.append({
            "account_id": row["account_id"],
            "number": row["marketplace_id"] or row["id"],
            "goods": row["goods"] or "",
            "quantity": row["items_count"] or 0,
            "deadline": deadline,
            "deadline_local": local_time(deadline),
            "urgency": urgency_of(deadline),
            "status_label": "Собран" if packed else STATUS_LABELS.get(status, status),
            # Возврат в работу сборщика по заказам не входит: он в своём разделе.
            "in_work": not packed and status != STATUS_ON_RETURN,
        })
    return feed
