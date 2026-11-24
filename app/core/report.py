"""Отчёт об отгруженных товарах.

Строка появляется не при открытии заказа, а в момент, когда сошлась пара
«штрихкод товара → номер отправления»: сборщик отсканировал товар, панель
нашла его в составе активного отправления и зачла. Ошибки сборщика пишутся
туда же с пометкой, чтобы потом было видно, где именно люди путаются.

Отчётный день закрывается в заданное время (по умолчанию 18:00 по Москве):
всё, что отсканировано после отсечки, попадает уже в следующий день — так
отчёт совпадает с фактической отгрузкой.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any

from . import accounts, db
from .config import settings

KV_CUTOFF = "report_cutoff"
DEFAULT_CUTOFF = "18:00"

STATUS_OK = "ok"
STATUS_UNMATCHED = "unmatched"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"

STATUS_LABELS = {
    STATUS_OK: "Отгружен",
    STATUS_UNMATCHED: "Штрихкод не опознан",
    STATUS_ERROR: "Ошибка сборки",
    STATUS_CANCELLED: "Сборка отменена",
}

REASON_LABELS = {
    "assembly_cancelled": "Сборщик отменил сборку до её завершения",
    "wrong_product": "Товар не из этого отправления",
    "wrong_label": "Стикер другого отправления",
    "extra_product": "Лишний скан сверх количества",
    "unknown_barcode": "Штрихкод не найден в справочнике",
    "no_candidates": "Товар не нужен ни в одном отправлении",
}


# ------------------------------------------------------------------ отсечка дня
def get_cutoff() -> str:
    """Время закрытия отчётного дня в местном часовом поясе, «ЧЧ:ММ»."""
    raw = (db.kv_get(KV_CUTOFF) or "").strip()
    return raw if _parse_cutoff(raw) else DEFAULT_CUTOFF


def validate_cutoff(value: str) -> str:
    parsed = _parse_cutoff(value) if isinstance(value, str) else None
    if not parsed:
        raise ValueError("Время указывается как ЧЧ:ММ, например 18:00")
    return f'{parsed[0]:02d}:{parsed[1]:02d}'


def set_cutoff(value: str, user: dict | None = None) -> str:
    cleaned = validate_cutoff(value)
    db.kv_set(KV_CUTOFF, cleaned)
    db.log_event("report_cutoff_set", user=user, message=cleaned)
    return cleaned


def _parse_cutoff(value: str | None) -> tuple[int, int] | None:
    parts = (value or "").strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def _to_local(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc) + timedelta(hours=settings.timezone_offset)


def report_date(moment: datetime | str | None = None, cutoff: str | None = None) -> str:
    """Отчётный день сканирования: после отсечки счёт идёт уже следующему дню."""
    if moment is None:
        moment = datetime.now(timezone.utc)
    elif isinstance(moment, str):
        try:
            moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
        except ValueError:
            moment = datetime.now(timezone.utc)
    local = _to_local(moment).replace(tzinfo=None)
    hour, minute = _parse_cutoff(cutoff or get_cutoff()) or _parse_cutoff(DEFAULT_CUTOFF)
    day = local.date()
    if (local.hour, local.minute) >= (hour, minute):
        day = day + timedelta(days=1)
    return day.isoformat()


def is_closed(day: str) -> bool:
    """День закрыт, когда отсечка по нему уже прошла: отчёт стал итоговым."""
    return day < report_date()


def cutoff_hint() -> str:
    zone = "по Москве" if settings.timezone_offset == 3 else f"по времени склада (UTC+{settings.timezone_offset})"
    return f"день закрывается в {get_cutoff()} {zone}"


# ------------------------------------------------------------------ запись
def _insert(conn, account: dict, user: dict, **row: Any) -> None:
    now = db.now_iso()
    if row.get('status') != STATUS_ERROR:
        # A cancelled attempt retains its row for audit, but a new scan must
        # credit the current collector/day rather than hit INSERT OR IGNORE.
        conn.execute("UPDATE shipped_items SET status=?,reason=?,barcode=?,user_id=?,login=?,scanned_at=?,report_date=? "
                     "WHERE account_id=? AND posting_number=? AND item_key=? AND unit_no=? AND status='cancelled'",
                     (row.get('status') or STATUS_OK, row.get('reason'), row.get('barcode'), (user or {}).get('id'), (user or {}).get('login'),
                      now, report_date(now), account['id'], row.get('posting_number') or '', row['item_key'], row.get('unit_no') or 1))
    conn.execute(
        """
        INSERT OR IGNORE INTO shipped_items(
            account_id, marketplace, posting_number, item_key, sku, offer_id, name, barcode,
            unit_no, status, reason, user_id, login, scanned_at, report_date)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            account["id"],
            account.get("marketplace") or "ozon",
            row.get("posting_number") or "",
            row["item_key"],
            row.get("sku"),
            row.get("offer_id"),
            row.get("name"),
            row.get("barcode"),
            int(row["unit_no"]) if row.get("unit_no") is not None else 1,
            row.get("status") or STATUS_OK,
            row.get("reason"),
            (user or {}).get("id"),
            (user or {}).get("login"),
            now,
            report_date(now),
        ),
    )


def record_shipped(conn, account: dict, user: dict, posting_number: str, item: dict,
                   unit_no: int, barcode: str | None) -> None:
    """Пара сошлась: товар из состава отправления зачтён сборщику.

    Ключ единицы обычно SKU, но у Avito его нет — там передаётся «key» вида
    av:<идентификатор позиции>, иначе защита от дублей не за что зацепиться.
    """
    _insert(
        conn, account, user,
        posting_number=posting_number,
        item_key=str(item.get("key") or item.get("sku") or ""),
        sku=item.get("sku"),
        offer_id=item.get("offer_id"),
        name=item.get("name"),
        barcode=barcode,
        unit_no=unit_no,
        status=STATUS_OK,
    )


_NOT_COMPLETED = """NOT EXISTS (
 SELECT 1 FROM events done WHERE done.account_id=s.account_id AND done.posting_number=s.posting_number
 AND done.kind IN ('pack_complete','yandex_pack_complete','avito_pack_complete') AND done.at>=s.scanned_at
)"""


def cancel_assembly(conn, account_id: int, order_id: str, user: dict) -> None:
    from ..markets import registry
    account = accounts.get(account_id)
    market = registry.get(account['marketplace']) if account else None
    board = market.orders if market else None
    if not board:
        return
    order = conn.execute(f'SELECT local_state,claim_user_id FROM {board.table} WHERE account_id=? AND {board.key}=?',
                         (account_id, order_id)).fetchone()
    if not order or order['local_state'] == 'packed' or order['claim_user_id'] not in (None, user['id']):
        return
    conn.execute("UPDATE shipped_items AS s SET status='cancelled',reason='assembly_cancelled' "
                 "WHERE s.account_id=? AND s.posting_number=? AND s.user_id=? AND s.status IN ('ok','unmatched') AND " + _NOT_COMPLETED,
                 (account_id, order_id, user['id']))


def repair_cancellations(conn) -> None:
    """Repair old report rows only when an explicit cancellation proves it."""
    from ..markets import registry
    for market in registry.all_markets():
        board = market.orders
        if not board:
            continue
        conn.execute("UPDATE shipped_items AS s SET status='cancelled',reason='assembly_cancelled' "
                     "WHERE s.marketplace=? AND s.status IN ('ok','unmatched') AND " + _NOT_COMPLETED +
                     ' AND EXISTS(SELECT 1 FROM events cancelled WHERE cancelled.account_id=s.account_id '
                     'AND cancelled.posting_number=s.posting_number AND cancelled.user_id=s.user_id '
                     "AND cancelled.kind IN ('pack_release','yandex_pack_release','avito_pack_release') AND cancelled.at>=s.scanned_at) "
                     f'AND EXISTS(SELECT 1 FROM {board.table} o WHERE o.account_id=s.account_id AND o.{board.key}=s.posting_number '
                     "AND o.local_state<>'packed')", (market.code,))


def record_unmatched(conn, account: dict, user: dict, posting_number: str, barcode: str,
                     name: str | None = None) -> None:
    """Штрихкода нет в справочнике (обычное дело для Avito) — пишем что есть."""
    unit_no = next_unit(account["id"], posting_number, f"bc:{barcode}")
    _insert(
        conn, account, user,
        posting_number=posting_number,
        item_key=f"bc:{barcode}",
        barcode=barcode,
        name=name,
        unit_no=unit_no,
        status=STATUS_UNMATCHED,
    )


def record_error(conn, account: dict, user: dict, reason: str, *, posting_number: str = "",
                 barcode: str | None = None, sku: str | None = None,
                 name: str | None = None, offer_id: str | None = None) -> None:
    """Пересорт: не тот товар или не то отправление."""
    _insert(
        conn, account, user,
        posting_number=posting_number,
        item_key=str(sku or f"bc:{barcode or '?'}"),
        sku=sku,
        offer_id=offer_id,
        name=name,
        barcode=barcode,
        unit_no=0,
        status=STATUS_ERROR,
        reason=reason,
    )


def next_unit(account_id: int, posting_number: str, item_key: str) -> int:
    row = db.query_one(
        "SELECT COALESCE(MAX(unit_no), 0) AS n FROM shipped_items "
        "WHERE account_id = ? AND posting_number = ? AND item_key = ? AND status NOT IN (?,?)",
        (account_id, posting_number or "", item_key, STATUS_ERROR, STATUS_CANCELLED),
    )
    return int(row["n"]) + 1 if row else 1


# ------------------------------------------------------------------ чтение
def days(account_id: int | None = None, limit: int = 60) -> list[dict]:
    """Список отчётных дней со сводкой."""
    where = ["1 = 1"]
    params: list[Any] = []
    if account_id:
        where.append("s.account_id = ?")
        params.append(account_id)
    rows = db.query(
        f"""
        SELECT s.report_date AS day,
               SUM(CASE WHEN s.status = 'ok' THEN 1 ELSE 0 END) AS shipped,
               SUM(CASE WHEN s.status = 'unmatched' THEN 1 ELSE 0 END) AS unmatched,
               SUM(CASE WHEN s.status = 'error' THEN 1 ELSE 0 END) AS errors,
               COUNT(DISTINCT CASE WHEN s.status IN ('ok','unmatched') THEN s.posting_number END) AS postings,
               COUNT(DISTINCT CASE WHEN s.status<>'cancelled' THEN s.login END) AS people
        FROM shipped_items s
        WHERE {' AND '.join(where)}
        GROUP BY s.report_date
        ORDER BY s.report_date DESC
        LIMIT ?
        """,
        params + [limit],
    )
    return [{**dict(row), "closed": is_closed(row["day"])} for row in rows]


def rows(day: str, account_id: int | None = None, status: str | None = None) -> list[dict]:
    where = ["s.report_date = ?"]
    params: list[Any] = [day]
    if account_id:
        where.append("s.account_id = ?")
        params.append(account_id)
    if status in {STATUS_OK, STATUS_UNMATCHED, STATUS_ERROR, STATUS_CANCELLED}:
        where.append("s.status = ?")
        params.append(status)
    found = db.query(
        f"""
        SELECT s.*, a.title AS account_title
        FROM shipped_items s LEFT JOIN accounts a ON a.id = s.account_id
        WHERE {' AND '.join(where)}
        ORDER BY s.scanned_at, s.id
        """,
        params,
    )
    return [_view(row) for row in found]


def _view(row: Any) -> dict:
    item = dict(row)
    item["status_label"] = STATUS_LABELS.get(item["status"], item["status"])
    item["reason_label"] = REASON_LABELS.get(item.get("reason") or "", item.get("reason") or "")
    item["scanned_local"] = _to_local(
        datetime.fromisoformat(str(item["scanned_at"]).replace("Z", "+00:00"))
    ).strftime("%d.%m.%Y %H:%M:%S")
    return item


def totals(day: str, account_id: int | None = None) -> dict:
    data = {"shipped": 0, "unmatched": 0, "errors": 0, "postings": 0}
    for row in days(account_id=account_id, limit=400):
        if row["day"] == day:
            data.update({k: row[k] for k in ("shipped", "unmatched", "errors", "postings")})
            break
    return data


# ------------------------------------------------------------------ выгрузка
CSV_HEADER = [
    "Отчётный день",
    "Время скана",
    "Кабинет",
    "Площадка",
    "Отправление",
    "Артикул продавца",
    "SKU",
    "Товар",
    "Штрихкод",
    "Единица",
    "Результат",
    "Причина",
    "Сборщик",
]


# Excel и LibreOffice считают формулой всё, что начинается с этих символов, и
# выполняют её при открытии файла. Названия товаров и артикулы приходят из
# кабинета площадки, то есть их пишет не панель, — поэтому обезвреживаем.
_FORMULA_STARTERS = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value: Any) -> str:
    """Значение для CSV, которое таблица откроет как текст, а не как формулу."""
    text = "" if value is None else str(value)
    if text.startswith(_FORMULA_STARTERS):
        # Ведущий апостроф — принятый способ сказать таблице «это текст».
        return "'" + text
    return text


def to_csv(day: str, account_id: int | None = None, status: str | None = None) -> bytes:
    """CSV для Excel: разделитель «;» и BOM, иначе кириллица открывается кракозябрами."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for item in rows(day, account_id=account_id, status=status):
        writer.writerow([_csv_cell(value) for value in (
            item["report_date"],
            item["scanned_local"],
            item.get("account_title") or "",
            accounts.marketplace_title(item.get("marketplace") or "ozon"),
            item.get("posting_number") or "",
            item.get("offer_id") or "",
            item.get("sku") or "",
            item.get("name") or "",
            item.get("barcode") or "",
            item["unit_no"] if item["status"] != STATUS_ERROR else "",
            item["status_label"],
            item["reason_label"],
            item.get("login") or "",
        )])
    return "﻿".encode() + buffer.getvalue().encode("utf-8")
