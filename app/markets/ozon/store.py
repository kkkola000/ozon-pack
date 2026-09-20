"""Отправления и каталог Ozon: таблицы, разбор ответов, поля для шаблонов."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

from ...core import db
from ...core.store import (_dt, _raw_json, _text, claim_is_active,
                           hours_left, local_time)

SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
    account_id       INTEGER NOT NULL,
    posting_number   TEXT NOT NULL,
    order_id         INTEGER,
    order_number     TEXT,
    status           TEXT,
    substatus        TEXT,
    in_process_at    TEXT,
    shipment_date    TEXT,
    delivering_date  TEXT,
    delivery_method  TEXT,
    warehouse_id     INTEGER,
    warehouse_name   TEXT,
    tpl_provider     TEXT,
    tracking_number  TEXT,
    is_express       INTEGER DEFAULT 0,
    is_multibox      INTEGER DEFAULT 0,
    multi_box_qty    INTEGER DEFAULT 0,
    barcode_upper    TEXT,
    barcode_lower    TEXT,
    region           TEXT,
    city             TEXT,
    delivery_type    TEXT,
    payment_type     TEXT,
    is_premium       INTEGER DEFAULT 0,
    requires_mark    INTEGER DEFAULT 0,
    requires_gtd     INTEGER DEFAULT 0,
    items_count      INTEGER DEFAULT 0,
    positions_count  INTEGER DEFAULT 0,
    cancel_reason    TEXT,
    raw              TEXT,
    local_state      TEXT NOT NULL DEFAULT 'new',
    claim_user_id    INTEGER,
    claim_login      TEXT,
    claim_at         TEXT,
    printed_at       TEXT,
    print_count      INTEGER NOT NULL DEFAULT 0,
    -- Когда стикер выгрузили на компьютер. Самого файла панель не хранит: он
    -- уезжает в браузер и живёт там. Здесь только отметка, и по ней решается,
    -- пускать ли к сканированию: без стикеров сборку начинать нечем.
    label_saved_at   TEXT,
    packed_at        TEXT,
    packed_by        TEXT,
    shipped_at       TEXT,
    first_seen_at    TEXT,
    updated_at       TEXT,
    PRIMARY KEY (account_id, posting_number)
);
CREATE INDEX IF NOT EXISTS idx_postings_status ON postings(account_id, status, local_state);
CREATE INDEX IF NOT EXISTS idx_postings_shipment ON postings(account_id, shipment_date);

CREATE TABLE IF NOT EXISTS posting_items (
    account_id     INTEGER NOT NULL,
    posting_number TEXT NOT NULL,
    sku            TEXT NOT NULL,
    offer_id       TEXT,
    name           TEXT,
    quantity       INTEGER NOT NULL DEFAULT 1,
    price          TEXT,
    currency       TEXT,
    mandatory_mark INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, posting_number, sku)
);
CREATE INDEX IF NOT EXISTS idx_items_sku ON posting_items(account_id, sku);
CREATE INDEX IF NOT EXISTS idx_items_offer ON posting_items(account_id, offer_id);

CREATE TABLE IF NOT EXISTS products (
    account_id INTEGER NOT NULL,
    sku        TEXT NOT NULL,
    offer_id   TEXT,
    name       TEXT,
    image      TEXT,
    barcodes   TEXT,
    -- Товар в архиве Ozon: он не продаётся, и в разделе «Товары» его быть не
    -- должно. Строку при этом не удаляем — её штрихкоды могут понадобиться,
    -- если архивный товар остался в несобранном заказе.
    archived   INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (account_id, sku)
);
CREATE INDEX IF NOT EXISTS idx_products_live ON products(account_id, archived);

CREATE TABLE IF NOT EXISTS product_barcodes (
    account_id INTEGER NOT NULL,
    barcode    TEXT NOT NULL,
    sku        TEXT NOT NULL,
    PRIMARY KEY (account_id, barcode)
);
CREATE INDEX IF NOT EXISTS idx_barcodes_sku ON product_barcodes(account_id, sku);

-- Набор: товар площадки, который физически собирается из нескольких разных
-- товаров со своими штрихкодами. Площадка о составе не знает — в отправлении
-- стоит одна позиция с одним SKU, а сборщик сканирует то, что лежит на полке.
-- Без состава такой скан был бы «товар не из этого отправления».
"""


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
            _raw_json(raw),
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


def product_key(item: dict) -> str:
    """SKU карточки так, как его сохраняет панель.

    Вынесено, чтобы загрузка каталога сверялась по тому же ключу, по которому
    идёт запись: разойдись они — и каждый обход отправлял бы живой товар в
    архив.
    """
    return str(item.get("sku") or item.get("id") or "")


def upsert_products(conn: sqlite3.Connection, account_id: int, items: Iterable[dict]) -> int:
    """Карточки товаров: имя, фото и штрихкоды для сканирования."""
    count = 0
    for item in items:
        sku = product_key(item)
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
