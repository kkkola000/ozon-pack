"""Преобразование ответов Ozon в строки БД и обратно в объекты для UI."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import db
from .config import settings

# Колонка raw хранит ответ площадки целиком — он нужен, когда на экране чего-то
# не хватает и надо понять, что именно прислали. Контакты покупателя панель при
# этом не показывает нигде, поэтому в базу они и не попадают: по 152-ФЗ хранить
# персональные данные без цели нельзя, а база лежит на складском сервере.
_CONTACT_KEY = re.compile(r"phone|email|passport|телефон|почта|паспорт", re.IGNORECASE)
_MAX_CLEAN_DEPTH = 8
# Число получения, каким его выбирают в календаре. Всё, что на это не похоже,
# в акт не попадёт ни при каком выборе даты — поэтому и проверяем.
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def without_contacts(value: Any, depth: int = 0) -> Any:
    """Копия ответа площадки без телефонов и почты — то, что уходит в колонку raw."""
    if depth > _MAX_CLEAN_DEPTH:
        return value
    if isinstance(value, dict):
        return {
            key: without_contacts(item, depth + 1)
            for key, item in value.items()
            if not _CONTACT_KEY.search(str(key))
        }
    if isinstance(value, list):
        return [without_contacts(item, depth + 1) for item in value]
    return value


def _raw_json(raw: dict) -> str:
    return json.dumps(without_contacts(raw), ensure_ascii=False)


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


def _moment(value: Any) -> str | None:
    """То же, что _dt, но нераспознанное — это None, а не исходная строка.

    _dt на всякий случай отдаёт строку как есть: дату показывают, и потерять её
    хуже, чем показать в чужом виде. Для момента получения так нельзя: из него
    считается число акта, и строка вроде «15.09.2026 12:30» дала бы «число»,
    которого нет ни в одном календаре. Возврат получен — и не находится нигде.
    """
    moment = _dt(value)
    if not moment:
        return None
    try:
        datetime.fromisoformat(moment.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment


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

    from .options import get_received_statuses, get_returns_statuses

    sys_name = _text(status.get("sys_name")) or ""
    # Готов к выдаче ровно тогда, когда Ozon сообщает нужный статус
    # (по умолчанию ArrivedAtReturnPlace — «В пункте выдачи»).
    is_ready = 1 if sys_name in set(get_returns_statuses()) else 0
    # «Получен» — возврат уже у нас, и по нему нужна отметка. Один и тот же
    # статус в обоих списках означает «ещё к выдаче»: сборщик за ним едет, а
    # класть в акт то, что не забрали, нельзя.
    received = not is_ready and sys_name in set(get_received_statuses())

    now = db.now_iso()
    # Когда возврат получили — берём у площадки, а не ставим «сейчас». Ozon
    # отдаёт по статусу «Получен» весь архив, и с временем «сейчас» первое же
    # обновление объявило бы полученными сегодня тысячи старых возвратов —
    # ровно один такой акт на 2446 позиций и получился в версии 1.17.
    #
    # Число — по final_moment, «прибыл на фулфилмент или выдан продавцу»: это
    # сам момент получения. change_moment запасной, это лишь последняя смена
    # статуса: она бывает раньше выдачи — статус перещёлкнулся 18-го, а на
    # руки возврат отдали 19-го, и в акт он вставал не тем числом.
    platform_moment = (
        _moment(logistic.get("final_moment")) or _moment(visual.get("change_moment"))
    ) if received else None
    if platform_moment and platform_moment > now:
        platform_moment = now
    # «Сейчас» — только если у площадки внятного момента нет вовсе.
    received_at = platform_moment or (now if received else None)
    # Число обязано быть числом: по нему и только по нему возврат попадает в
    # акт. Если из момента его не вышло, ставим сегодняшнее — возврат лучше
    # положить в акт не за тот день, чем потерять с экрана совсем.
    received_day = local_day(received_at) if received_at else None
    if received_at and not _DAY.fullmatch(received_day or ""):
        received_day = local_day(now)

    existing = conn.execute(
        "SELECT first_seen_at FROM returns WHERE account_id = ? AND id = ?", (account_id, return_id)
    ).fetchone()
    conn.execute(
        """
        INSERT INTO returns (
            account_id, id, type, scheme, status_sys, status_name, order_id, order_number, posting_number, sku, offer_id,
            product_name, quantity, price, currency, place_name, place_address, target_place_name, return_reason,
            return_date, final_moment, status_changed_at, arrived_at, storage_until, storage_sum, barcode,
            is_ready, raw, first_seen_at, updated_at, received_at, received_day
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(account_id, id) DO UPDATE SET
            type = excluded.type, scheme = excluded.scheme, status_sys = excluded.status_sys,
            status_name = excluded.status_name, order_id = excluded.order_id, order_number = excluded.order_number,
            posting_number = excluded.posting_number, sku = excluded.sku, offer_id = excluded.offer_id,
            product_name = excluded.product_name, quantity = excluded.quantity, price = excluded.price,
            currency = excluded.currency, place_name = excluded.place_name, place_address = excluded.place_address,
            target_place_name = excluded.target_place_name, return_reason = excluded.return_reason,
            return_date = excluded.return_date, final_moment = excluded.final_moment,
            status_changed_at = excluded.status_changed_at, arrived_at = excluded.arrived_at,
            storage_until = excluded.storage_until, storage_sum = excluded.storage_sum, barcode = excluded.barcode,
            is_ready = excluded.is_ready, raw = excluded.raw, updated_at = excluded.updated_at,
            -- Здесь момент получения только дописывается, но не перебивается:
            -- в excluded он может оказаться «сейчас», а Ozon отдаёт статус
            -- «Получен» и на следующих обновлениях — каждое двигало бы дату
            -- вперёд. Настоящий момент площадки ставит отдельный UPDATE ниже.
            received_at = COALESCE(returns.received_at, excluded.received_at),
            received_day = COALESCE(returns.received_day, excluded.received_day)
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
            _moment(visual.get("change_moment")),
            _moment(storage.get("arrived_moment")),
            _dt(storage.get("utilization_forecast_date")),
            _text((storage.get("sum") or {}).get("price")),
            _text(logistic.get("barcode")),
            is_ready,
            _raw_json(raw),
            (existing["first_seen_at"] if existing else now) or now,
            now,
            received_at,
            received_day,
        ),
    )
    # Момент от площадки — величина постоянная, и если он приехал позже или
    # изменился, число надо поправить. Без этого первая запись залипала: статус
    # сменился 18-го, выдали 19-го, а возврат так и оставался за 18-м, сколько
    # ни обновляй. «Сейчас» сюда не попадает — им затирать нечего.
    #
    # Возврат из подтверждённого акта не трогаем: там работа закрыта, и менять
    # под ней число значило бы переписывать уже подписанный документ.
    if platform_moment:
        conn.execute(
            "UPDATE returns SET received_at = ?, received_day = ? "
            "WHERE account_id = ? AND id = ? AND (act_id IS NULL OR act_id IN "
            "(SELECT id FROM return_acts WHERE confirmed_at IS NULL))",
            (platform_moment, local_day(platform_moment), account_id, return_id),
        )
        # Число переехало, а возврат остался в акте за прежнее — и «Обновить»
        # не помогало: в акте за 18-е так и висел возврат, полученный 19-го.
        # Освобождаем, чтобы он попал в акт за своё число. Акт при этом не
        # составляется: его по-прежнему заводит человек кнопкой.
        #
        # Отмеченный возврат остаётся на месте: отметку ставил сборщик, держа
        # возврат в руках, и переносить её работу в другой акт нельзя.
        conn.execute(
            "UPDATE returns SET act_id = NULL WHERE account_id = ? AND id = ? "
            "AND mark IS NULL AND act_id IN (SELECT id FROM return_acts "
            "WHERE confirmed_at IS NULL AND received_day IS NOT NULL "
            "AND received_day <> ?)",
            (account_id, return_id, local_day(platform_moment)),
        )
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


def local_day(value: str | None = None) -> str:
    """ISO-UTC -> местная дата «ГГГГ-ММ-ДД». Без аргумента — сегодняшняя.

    Момент хранится в UTC, а человек называет число по часам склада: вечерняя
    поездка в UTC+3 иначе попала бы во вчерашний день.
    """
    return local_time(value or db.now_iso(), "%Y-%m-%d")


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


# Отметка сборщика о возврате. Ставит её человек в пункте выдачи, площадка о
# ней не знает: Ozon и Avito в своих статусах различают только «лежит в ПВЗ» и
# «уехал дальше», а принял ли сборщик товар и что с ним было не так — видно
# только на месте.
RETURN_MARKS = {"ok": "Принят", "bad": "Не принят"}
RETURN_MARK_SIGNS = {"ok": "✓", "bad": "✗"}


def mark_label(code: str | None) -> str:
    return RETURN_MARKS.get(code or "", "")


def _with_mark(data: dict) -> dict:
    data["mark_label"] = mark_label(data.get("mark"))
    data["mark_sign"] = RETURN_MARK_SIGNS.get(data.get("mark") or "", "")
    data["mark_at_local"] = local_time(data.get("mark_at"))
    return data


def return_view(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    data.pop("raw", None)
    data["status_label"] = data.get("status_name") or RETURN_STATUS_LABELS.get(data.get("status_sys") or "", "")
    # Когда возврат стал готов к выдаче. Только показываем: по нему видно, что
    # лежит в пункте давно, а что привезли сегодня. Список по нему не строится
    # и не сортируется — состав раздела решают отмеченные статусы.
    data["arrived_local"] = local_time(data.get("arrived_at"), "%d.%m.%Y") if data.get("arrived_at") else ""
    return _with_mark(data)


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

    from ..markets.avito import client as avito_client

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
    from ..markets.avito.client import RETURN_STATUS_LABELS, SERVICE_LABELS, STATUS_LABELS

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
    # Кто и когда собрал — во вкладке «Собранные» это главное, что нужно знать:
    # площадка о сборке не знает, и спросить, кроме панели, негде.
    data["packed_at_local"] = local_time(data.get("packed_at"))
    if with_items:
        data["items"] = avito_items(data["account_id"], data["id"])
    return _with_mark(data)


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


def offer_barcodes(offer_id: str | None) -> list[str]:
    """Штрихкоды товара по артикулу — из каталога любого кабинета панели.

    Кабинет не ограничиваем намеренно: каталог наполняется из Ozon, а заказ
    пришёл из Маркета. Артикул у продавца один на все площадки, и искать
    штрихкод только в своём кабинете значило бы не найти его никогда.
    """
    if not offer_id:
        return []
    rows = db.query(
        "SELECT DISTINCT b.barcode FROM product_barcodes b "
        "JOIN products p ON p.account_id = b.account_id AND p.sku = b.sku "
        "WHERE p.offer_id = ? ORDER BY b.barcode",
        (offer_id,),
    )
    return [row["barcode"] for row in rows]


def yandex_view(row: sqlite3.Row | dict, *, with_items: bool = True) -> dict:
    """Строка БД -> объект для шаблона: подписи, срочность, состав."""
    from ..markets.yandex.client import DELIVERY_LABELS, SUBSTATUS_LABELS

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
    data["deadline_local"] = local_time(deadline, "%d.%m") if deadline else ""
    data["hours_left"] = round(left, 1) if left is not None else None
    data["urgency"] = urgency
    data["created_local"] = local_time(data.get("created_at_api"))
    data["packed_at_local"] = local_time(data.get("packed_at"))
    data["claim_active"] = claim_is_active(data.get("claim_at"))
    if with_items:
        data["items"] = yandex_items(data["account_id"], data["id"])
    return data
