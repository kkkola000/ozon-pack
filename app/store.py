"""Преобразование ответов Ozon в строки БД и обратно в объекты для UI."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import db
from .config import settings

# Рабочие статусы FBS: то, что сборщик видит в панели.
STATUS_AWAITING_PACKAGING = "awaiting_packaging"
STATUS_AWAITING_DELIVER = "awaiting_deliver"
WORK_STATUSES = (STATUS_AWAITING_PACKAGING, STATUS_AWAITING_DELIVER)

STATUS_LABELS = {
    "acceptance_in_progress": "Идёт приёмка",
    "arbitration": "Арбитраж",
    "awaiting_approve": "Ожидает подтверждения",
    "awaiting_packaging": "Ожидает сборки",
    "awaiting_deliver": "Ожидает отгрузки",
    "awaiting_registration": "Ожидает регистрации",
    "awaiting_verification": "Создано",
    "cancelled": "Отменено",
    "client_arbitration": "Клиентский арбитраж",
    "delivered": "Доставлено",
    "delivering": "Доставляется",
    "driver_pickup": "У водителя",
    "not_accepted": "Не принято на сортировочном центре",
    "sent_by_seller": "Отправлено продавцом",
}

LOCAL_STATE_LABELS = {
    "new": "Не собрано",
    "packed": "Собрано",
    "cancelled": "Отменено",
}

# Возврат «готов к выдаче» — статусы из /v1/returns/list (visual.status.sys_name).
RETURN_STATUS_LABELS = {
    "ArrivedAtReturnPlace": "В пункте выдачи",
    "MovingToSeller": "Едет к продавцу",
    "WaitingShipment": "Ожидает отгрузки",
    "ReturningByCourier": "Везёт курьер",
    "ReceivedBySeller": "Получен продавцом",
    "MovingToOzon": "Едет на склад Ozon",
    "ReturnedToOzon": "На складе Ozon",
    "Utilizing": "На утилизации",
    "Utilized": "Утилизирован",
    "Cancelled": "Отменён",
}
RETURN_TAKEN_STATUSES = {"ReceivedBySeller", "Utilized", "Utilizing", "Cancelled"}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    return str(value)


def _dt(value: Any) -> str | None:
    """Нормализовать дату Ozon к ISO-8601 UTC (строки сортируются лексикографически)."""
    raw = _text(value)
    if not raw or raw.startswith("0001-01-01"):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def upsert_posting(conn: sqlite3.Connection, account_id: int, raw: dict) -> str:
    """Сохранить отправление, не затирая локальное состояние сборки."""
    number = raw.get("posting_number")
    if not number:
        raise ValueError("В ответе Ozon нет posting_number")

    delivery = raw.get("delivery_method") or {}
    analytics = raw.get("analytics_data") or {}
    barcodes = raw.get("barcodes") or {}
    requirements = raw.get("requirements") or {}
    cancellation = raw.get("cancellation") or {}
    products = raw.get("products") or []

    items_count = sum(int(p.get("quantity") or 0) for p in products)
    now = db.now_iso()
    existing = conn.execute(
        "SELECT status, local_state, first_seen_at FROM postings WHERE account_id = ? AND posting_number = ?",
        (account_id, number),
    ).fetchone()

    status = _text(raw.get("status"))
    local_state = existing["local_state"] if existing else "new"
    if status == "cancelled":
        local_state = "cancelled"
    elif local_state == "cancelled" and status in WORK_STATUSES:
        local_state = "new"

    conn.execute(
        """
        INSERT INTO postings (
            account_id, posting_number, order_id, order_number, status, substatus, in_process_at, shipment_date,
            delivering_date, delivery_method, warehouse_id, warehouse_name, tpl_provider, tracking_number,
            is_express, is_multibox, multi_box_qty, barcode_upper, barcode_lower, region, city,
            delivery_type, payment_type, is_premium, requires_mark, requires_gtd, items_count,
            positions_count, cancel_reason, raw, local_state, first_seen_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(account_id, posting_number) DO UPDATE SET
            order_id = excluded.order_id,
            order_number = excluded.order_number,
            status = excluded.status,
            substatus = excluded.substatus,
            in_process_at = excluded.in_process_at,
            shipment_date = excluded.shipment_date,
            delivering_date = excluded.delivering_date,
            delivery_method = excluded.delivery_method,
            warehouse_id = excluded.warehouse_id,
            warehouse_name = excluded.warehouse_name,
            tpl_provider = excluded.tpl_provider,
            tracking_number = excluded.tracking_number,
            is_express = excluded.is_express,
            is_multibox = excluded.is_multibox,
            multi_box_qty = excluded.multi_box_qty,
            barcode_upper = excluded.barcode_upper,
            barcode_lower = excluded.barcode_lower,
            region = excluded.region,
            city = excluded.city,
            delivery_type = excluded.delivery_type,
            payment_type = excluded.payment_type,
            is_premium = excluded.is_premium,
            requires_mark = excluded.requires_mark,
            requires_gtd = excluded.requires_gtd,
            items_count = excluded.items_count,
            positions_count = excluded.positions_count,
            cancel_reason = excluded.cancel_reason,
            raw = excluded.raw,
            local_state = excluded.local_state,
            updated_at = excluded.updated_at
        """,
        (
            account_id,
            number,
            raw.get("order_id"),
            _text(raw.get("order_number")),
            status,
            _text(raw.get("substatus")),
            _dt(raw.get("in_process_at")),
            _dt(raw.get("shipment_date")),
            _dt(raw.get("delivering_date")),
            _text(delivery.get("name")),
            delivery.get("warehouse_id"),
            _text(delivery.get("warehouse")) or _text(analytics.get("warehouse")),
            _text(delivery.get("tpl_provider")) or _text(analytics.get("tpl_provider")),
            _text(raw.get("tracking_number")),
            1 if raw.get("is_express") else 0,
            1 if raw.get("is_multibox") else 0,
            int(raw.get("multi_box_qty") or 0),
            _text(barcodes.get("upper_barcode")),
            _text(barcodes.get("lower_barcode")),
            _text(analytics.get("region")),
            _text(analytics.get("city")),
            _text(analytics.get("delivery_type")),
            _text(analytics.get("payment_type_group_name")),
            1 if analytics.get("is_premium") else 0,
            1 if (requirements.get("products_requiring_mandatory_mark") or []) else 0,
            1 if (requirements.get("products_requiring_gtd") or []) else 0,
            items_count,
            len(products),
            _text(cancellation.get("cancel_reason")),
            json.dumps(raw, ensure_ascii=False),
            local_state,
            (existing["first_seen_at"] if existing else now) or now,
            now,
        ),
    )

    mandatory = {str(s) for s in (requirements.get("products_requiring_mandatory_mark") or [])}
    conn.execute("DELETE FROM posting_items WHERE account_id = ? AND posting_number = ?", (account_id, number))
    for product in products:
        sku = str(product.get("sku") or "")
        if not sku:
            continue
        conn.execute(
            """
            INSERT INTO posting_items(account_id, posting_number, sku, offer_id, name, quantity, price, currency, mandatory_mark)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account_id, posting_number, sku) DO UPDATE SET
                offer_id = excluded.offer_id, name = excluded.name, quantity = excluded.quantity,
                price = excluded.price, currency = excluded.currency, mandatory_mark = excluded.mandatory_mark
            """,
            (
                account_id,
                number,
                sku,
                _text(product.get("offer_id")),
                _text(product.get("name")),
                int(product.get("quantity") or 1),
                _text(product.get("price")),
                _text(product.get("currency_code")),
                1 if sku in mandatory or (product.get("mandatory_mark") or []) else 0,
            ),
        )
    return number


def upsert_products(conn: sqlite3.Connection, account_id: int, items: Iterable[dict]) -> int:
    """Карточки товаров: имя, фото и штрихкоды для сканирования."""
    count = 0
    for item in items:
        sku = str(item.get("sku") or item.get("id") or "")
        if not sku:
            continue
        barcodes = [str(b).strip() for b in (item.get("barcodes") or []) if str(b).strip()]
        primary = item.get("primary_image") or item.get("images") or []
        image = primary[0] if isinstance(primary, list) and primary else _text(primary if isinstance(primary, str) else None)
        conn.execute(
            """
            INSERT INTO products(account_id, sku, offer_id, name, image, barcodes, updated_at) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(account_id, sku) DO UPDATE SET offer_id = excluded.offer_id, name = excluded.name,
                image = excluded.image, barcodes = excluded.barcodes, updated_at = excluded.updated_at
            """,
            (account_id, sku, _text(item.get("offer_id")), _text(item.get("name")), image,
             json.dumps(barcodes, ensure_ascii=False), db.now_iso()),
        )
        for barcode in barcodes:
            conn.execute(
                "INSERT INTO product_barcodes(account_id, barcode, sku) VALUES(?, ?, ?) "
                "ON CONFLICT(account_id, barcode) DO UPDATE SET sku = excluded.sku",
                (account_id, barcode, sku),
            )
        count += 1
    return count


def upsert_return(conn: sqlite3.Connection, account_id: int, raw: dict) -> str:
    """Возврат из /v1/returns/list (единый метод для FBO и FBS)."""
    return_id = str(raw.get("id") or "")
    if not return_id:
        raise ValueError("В ответе Ozon нет id возврата")

    product = raw.get("product") or {}
    place = raw.get("place") or {}
    target = raw.get("target_place") or {}
    storage = raw.get("storage") or {}
    logistic = raw.get("logistic") or {}
    visual = raw.get("visual") or {}
    status = visual.get("status") or {}
    price = product.get("price") or {}

    from .options import get_returns_statuses

    sys_name = _text(status.get("sys_name")) or ""
    # Готов к выдаче ровно тогда, когда Ozon сообщает нужный статус
    # (по умолчанию ArrivedAtReturnPlace — «В пункте выдачи»).
    is_ready = 1 if sys_name in set(get_returns_statuses()) else 0

    now = db.now_iso()
    existing = conn.execute(
        "SELECT first_seen_at, taken_at FROM returns WHERE account_id = ? AND id = ?", (account_id, return_id)
    ).fetchone()
    conn.execute(
        """
        INSERT INTO returns (
            account_id, id, type, scheme, status_sys, status_name, order_id, order_number, posting_number, sku, offer_id,
            product_name, quantity, price, currency, place_name, place_address, target_place_name, return_reason,
            return_date, final_moment, storage_until, storage_sum, barcode, is_ready, raw, first_seen_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(account_id, id) DO UPDATE SET
            type = excluded.type, scheme = excluded.scheme, status_sys = excluded.status_sys,
            status_name = excluded.status_name, order_id = excluded.order_id, order_number = excluded.order_number,
            posting_number = excluded.posting_number, sku = excluded.sku, offer_id = excluded.offer_id,
            product_name = excluded.product_name, quantity = excluded.quantity, price = excluded.price,
            currency = excluded.currency, place_name = excluded.place_name, place_address = excluded.place_address,
            target_place_name = excluded.target_place_name, return_reason = excluded.return_reason,
            return_date = excluded.return_date, final_moment = excluded.final_moment,
            storage_until = excluded.storage_until, storage_sum = excluded.storage_sum, barcode = excluded.barcode,
            is_ready = excluded.is_ready, raw = excluded.raw, updated_at = excluded.updated_at
        """,
        (
            account_id,
            return_id,
            _text(raw.get("type")),
            _text(raw.get("schema")),
            sys_name or None,
            _text(status.get("display_name")) or RETURN_STATUS_LABELS.get(sys_name, sys_name),
            raw.get("order_id"),
            _text(raw.get("order_number")),
            _text(raw.get("posting_number")),
            _text(product.get("sku")),
            _text(product.get("offer_id")),
            _text(product.get("name")),
            int(product.get("quantity") or 1),
            _text(price.get("price")),
            _text(price.get("currency_code")),
            _text(place.get("name")),
            _text(place.get("address")),
            _text(target.get("name")),
            _text(raw.get("return_reason_name")),
            _dt(logistic.get("return_date")),
            _dt(logistic.get("final_moment")),
            _dt(storage.get("utilization_forecast_date")),
            _text((storage.get("sum") or {}).get("price")),
            _text(logistic.get("barcode")),
            is_ready,
            json.dumps(raw, ensure_ascii=False),
            (existing["first_seen_at"] if existing else now) or now,
            now,
        ),
    )
    # Возврат уехал из пункта выдачи — снимаем локальную отметку «забрали».
    if existing and existing["taken_at"] and sys_name in RETURN_TAKEN_STATUSES:
        conn.execute("UPDATE returns SET is_ready = 0 WHERE account_id = ? AND id = ?", (account_id, return_id))
    return return_id


# ------------------------------------------------------------------ чтение для UI
def posting_items(account_id: int, posting_number: str) -> list[dict]:
    rows = db.query(
        """
        SELECT i.*, p.image, p.barcodes
        FROM posting_items i LEFT JOIN products p ON p.sku = i.sku AND p.account_id = i.account_id
        WHERE i.account_id = ? AND i.posting_number = ? ORDER BY i.name
        """,
        (account_id, posting_number),
    )
    items = []
    for row in rows:
        item = dict(row)
        item["barcodes"] = json.loads(item.get("barcodes") or "[]")
        items.append(item)
    return items


def hours_left(shipment_date: str | None) -> float | None:
    if not shipment_date:
        return None
    try:
        target = datetime.fromisoformat(shipment_date)
    except ValueError:
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return (target - datetime.now(timezone.utc)).total_seconds() / 3600


def local_time(value: str | None, fmt: str = "%d.%m %H:%M") -> str:
    """ISO-UTC -> локальное время склада (TZ_OFFSET_HOURS)."""
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    shifted = moment.astimezone(timezone.utc) + timedelta(hours=settings.timezone_offset)
    return shifted.strftime(fmt)


def posting_view(row: sqlite3.Row | dict, *, with_items: bool = True) -> dict:
    """Строка БД -> объект для шаблона/JSON: подписи, срочность, локальный статус."""
    data = dict(row)
    number = data["posting_number"]
    left = hours_left(data.get("shipment_date"))
    if left is None:
        urgency = "none"
    elif left < 0:
        urgency = "overdue"
    elif left < 6:
        urgency = "urgent"
    elif left < 24:
        urgency = "soon"
    else:
        urgency = "ok"
    data["hours_left"] = round(left, 1) if left is not None else None
    data["urgency"] = urgency
    data["status_label"] = STATUS_LABELS.get(data.get("status") or "", data.get("status") or "")
    data["local_state_label"] = LOCAL_STATE_LABELS.get(data.get("local_state") or "new", data.get("local_state"))
    data["claim_active"] = claim_is_active(data.get("claim_at"))
    data["shipment_date_local"] = local_time(data.get("shipment_date"))
    data["packed_at_local"] = local_time(data.get("packed_at"))
    if with_items:
        data["items"] = posting_items(data["account_id"], number)
    data.pop("raw", None)
    return data


def claim_is_active(claim_at: str | None) -> bool:
    if not claim_at:
        return False
    try:
        moment = datetime.fromisoformat(claim_at)
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - moment < timedelta(minutes=settings.claim_ttl_minutes)


def return_view(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    data.pop("raw", None)
    data["status_label"] = data.get("status_name") or RETURN_STATUS_LABELS.get(data.get("status_sys") or "", "")
    return data


# ------------------------------------------------------------------ заказы Avito
def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def upsert_avito_order(conn: sqlite3.Connection, account_id: int, raw: dict) -> str:
    """Заказ из GET /order-management/1/orders, не затирая локальные отметки."""
    order_id = str(raw.get("id") or "")
    if not order_id:
        raise ValueError("В ответе Avito нет id заказа")

    delivery = raw.get("delivery") or {}
    buyer = delivery.get("buyerInfo") or {}
    terminal = delivery.get("terminalInfo") or {}
    prices = raw.get("prices") or {}
    schedules = raw.get("schedules") or {}
    items = raw.get("items") or []
    actions = [a.get("name") for a in (raw.get("availableActions") or []) if a.get("name")]

    now = db.now_iso()
    existing = conn.execute(
        "SELECT first_seen_at, local_state FROM avito_orders WHERE account_id = ? AND id = ?", (account_id, order_id)
    ).fetchone()

    conn.execute(
        """
        INSERT INTO avito_orders (
            account_id, id, marketplace_id, status, service_type, service_name, dispatch_number, tracking_number,
            terminal_code, terminal_address, buyer_name, buyer_phone, confirm_till, ship_till, delivery_date,
            price, total, delivery_price, commission, items_count, positions_count, actions,
            created_at_api, updated_at_api, raw, first_seen_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(account_id, id) DO UPDATE SET
            marketplace_id = excluded.marketplace_id, status = excluded.status,
            service_type = excluded.service_type, service_name = excluded.service_name,
            dispatch_number = excluded.dispatch_number, tracking_number = excluded.tracking_number,
            terminal_code = excluded.terminal_code, terminal_address = excluded.terminal_address,
            buyer_name = excluded.buyer_name, buyer_phone = excluded.buyer_phone,
            confirm_till = excluded.confirm_till, ship_till = excluded.ship_till,
            delivery_date = excluded.delivery_date, price = excluded.price, total = excluded.total,
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
            _text(terminal.get("code")),
            _text(terminal.get("address")),
            _text(buyer.get("fullName")),
            _text(buyer.get("phoneNumber")),
            _dt(schedules.get("confirmTill")),
            _dt(schedules.get("shipTill")),
            _dt(schedules.get("deliveryDate")) or _dt(schedules.get("deliveryDateMax")),
            _num(prices.get("price")),
            _num(prices.get("total")),
            _num(prices.get("delivery")),
            _num(prices.get("commission")),
            sum(int(i.get("count") or 0) for i in items),
            len(items),
            json.dumps(actions, ensure_ascii=False),
            _dt(raw.get("createdAt")),
            _dt(raw.get("updatedAt")),
            json.dumps(raw, ensure_ascii=False),
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
    from .avito import SERVICE_LABELS, STATUS_LABELS

    data = dict(row)
    data.pop("raw", None)
    status = data.get("status") or ""
    data["status_label"] = STATUS_LABELS.get(status, status)
    data["service_label"] = SERVICE_LABELS.get(data.get("service_type") or "", data.get("service_type") or "")
    try:
        data["actions"] = json.loads(data.get("actions") or "[]")
    except ValueError:
        data["actions"] = []
    # Срок берём тот, который сейчас поджимает: подтверждение или отправка.
    deadline = data.get("confirm_till") if status == "on_confirmation" else data.get("ship_till")
    left = hours_left(deadline)
    if left is None:
        urgency = "none"
    elif left < 0:
        urgency = "overdue"
    elif left < 6:
        urgency = "urgent"
    elif left < 24:
        urgency = "soon"
    else:
        urgency = "ok"
    data["deadline"] = deadline
    data["deadline_local"] = local_time(deadline)
    data["hours_left"] = round(left, 1) if left is not None else None
    data["urgency"] = urgency
    data["created_local"] = local_time(data.get("created_at_api"))
    if with_items:
        data["items"] = avito_items(data["account_id"], data["id"])
    return data
