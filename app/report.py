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
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import db
from .config import settings

KV_CUTOFF = "report_cutoff"
DEFAULT_CUTOFF = "18:00"

STATUS_OK = "ok"
STATUS_UNMATCHED = "unmatched"
STATUS_ERROR = "error"

STATUS_LABELS = {
    STATUS_OK: "Отгружен",
    STATUS_UNMATCHED: "Штрихкод не опознан",
    STATUS_ERROR: "Ошибка сборки",
}

REASON_LABELS = {
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


def set_cutoff(value: str, user: dict | None = None) -> str:
    parsed = _parse_cutoff(value)
    if not parsed:
        raise ValueError("Время указывается как ЧЧ:ММ, например 18:00")
    cleaned = "%02d:%02d" % parsed
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


def local_now() -> datetime:
    """Текущее время склада (TZ_OFFSET_HOURS)."""
    return _to_local(datetime.now(timezone.utc))


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
        "WHERE account_id = ? AND posting_number = ? AND item_key = ? AND status <> ?",
        (account_id, posting_number or "", item_key, STATUS_ERROR),
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
               COUNT(DISTINCT CASE WHEN s.status <> 'error' THEN s.posting_number END) AS postings,
               COUNT(DISTINCT s.login) AS people
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
    if status in {STATUS_OK, STATUS_UNMATCHED, STATUS_ERROR}:
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


def to_csv(day: str, account_id: int | None = None, status: str | None = None) -> bytes:
    """CSV для Excel: разделитель «;» и BOM, иначе кириллица открывается кракозябрами."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for item in rows(day, account_id=account_id, status=status):
        writer.writerow([
            item["report_date"],
            item["scanned_local"],
            item.get("account_title") or "",
            "Avito" if item.get("marketplace") == "avito" else "Ozon",
            item.get("posting_number") or "",
            item.get("offer_id") or "",
            item.get("sku") or "",
            item.get("name") or "",
            item.get("barcode") or "",
            item["unit_no"] if item["status"] != STATUS_ERROR else "",
            item["status_label"],
            item["reason_label"],
            item.get("login") or "",
        ])
    return "﻿".encode("utf-8") + buffer.getvalue().encode("utf-8")
