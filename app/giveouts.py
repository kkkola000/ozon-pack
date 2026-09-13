"""Акты выдачи возвратов, которые составляет Ozon.

Свой акт панель собирает из напечатанного листа (app/return_acts.py) — это
внутренний документ склада: кто поехал, что привёз, что принял. Ozon ведёт свой
учёт того же события и отдаёт его методами /v1/return/giveout/*. У Avito
такого нет вовсе.

Панель загружает акты Ozon и показывает рядом со своими — чтобы было с чем
сверить полученное. Автоматически их между собой не сшиваем: сопоставлять
документы двух сторон по составу можно только когда точно известны поля Ozon,
а они у этого метода менялись. Поэтому разобранное лежит в колонках, а ответ
целиком сохраняется в raw: если поля разъедутся, будет по чему чинить.
"""
from __future__ import annotations

import json
import logging

from . import db, ozon, store
from .ozon import OzonError

log = logging.getLogger("giveouts")

MAX_PAGES = 20
PAGE_LIMIT = 100

# Как Ozon называет одно и то же в разных версиях ответа.
ID_KEYS = ("giveout_id", "id")
STATUS_KEYS = ("giveout_status", "status", "state")
CREATED_KEYS = ("created_at", "created", "giveout_date", "date")
ITEM_LIST_KEYS = ("articles", "items", "products", "returns")

STATUS_LABELS = {
    "FORMED": "Сформирован",
    "CREATED": "Сформирован",
    "NEW": "Сформирован",
    "IN_PROGRESS": "В работе",
    "APPROVED": "Подтверждён",
    "COMPLETED": "Выдан",
    "DONE": "Выдан",
    "CANCELLED": "Отменён",
    "CANCELED": "Отменён",
    "EXPIRED": "Просрочен",
}


def _pick(raw: dict, names: tuple[str, ...]) -> str:
    for name in names:
        value = raw.get(name)
        if value not in (None, "", []):
            return str(value)
    return ""


def _items_of(info: dict) -> list[dict]:
    for name in ITEM_LIST_KEYS:
        value = info.get(name)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def status_label(code: str) -> str:
    return STATUS_LABELS.get((code or "").upper(), code or "")


def sync_account(account: dict) -> dict:
    """Загрузить акты выдачи кабинета. Мягко к отсутствию метода.

    Метод выдачи включён не у всех продавцов, и старые кабинеты отвечают на
    него ошибкой. Это не повод ронять синхронизацию возвратов: возвращаем
    причину, панель её показывает.
    """
    client = ozon.get_client(account)
    account_id = account["id"]
    saved = 0
    last_id = 0

    for _page in range(MAX_PAGES):
        try:
            giveouts, has_next = client.giveout_list(limit=PAGE_LIMIT, last_id=last_id)
        except OzonError as exc:
            log.warning("Акты выдачи недоступны: %s", exc)
            return {"giveouts": saved, "giveouts_error": exc.message}
        if not giveouts:
            break
        for raw in giveouts:
            giveout_id = _pick(raw, ID_KEYS)
            if not giveout_id:
                continue
            info: dict = {}
            try:
                info = client.giveout_info(giveout_id)
            except OzonError as exc:
                # Список дошёл, состав — нет. Акт всё равно сохраняем: сам факт
                # выдачи важнее её состава.
                log.info("Состав акта %s недоступен: %s", giveout_id, exc)
            merged = {**raw, **info}
            items = _items_of(merged)
            _upsert(account_id, giveout_id, merged, items)
            saved += 1
        last_id = _pick(giveouts[-1], ID_KEYS) or 0
        try:
            last_id = int(last_id)
        except (TypeError, ValueError):
            break
        if not has_next or not last_id:
            break

    return {"giveouts": saved}


def _upsert(account_id: int, giveout_id: str, raw: dict, items: list[dict]) -> None:
    now = db.now_iso()
    status = _pick(raw, STATUS_KEYS)
    existing = db.query_one(
        "SELECT first_seen_at FROM ozon_giveouts WHERE account_id = ? AND id = ?", (account_id, giveout_id)
    )
    db.execute(
        """
        INSERT INTO ozon_giveouts(account_id, id, status, status_label, created_at, items_count,
                                  items, raw, first_seen_at, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(account_id, id) DO UPDATE SET
            status = excluded.status, status_label = excluded.status_label,
            created_at = excluded.created_at, items_count = excluded.items_count,
            items = excluded.items, raw = excluded.raw, updated_at = excluded.updated_at
        """,
        (
            account_id, giveout_id, status or None, status_label(status) or None,
            _pick(raw, CREATED_KEYS) or None, len(items),
            json.dumps(items, ensure_ascii=False),
            json.dumps(raw, ensure_ascii=False),
            (existing["first_seen_at"] if existing else now) or now,
            now,
        ),
    )


def view(row) -> dict:
    data = dict(row)
    data.pop("raw", None)
    try:
        data["items"] = json.loads(data.get("items") or "[]")
    except ValueError:
        data["items"] = []
    data["created_local"] = store.local_time(data.get("created_at"))
    data["status_label"] = data.get("status_label") or status_label(data.get("status") or "")
    return data


def recent(account_id: int, limit: int = 20) -> list[dict]:
    rows = db.query(
        "SELECT * FROM ozon_giveouts WHERE account_id = ? "
        "ORDER BY (created_at IS NULL), created_at DESC, first_seen_at DESC LIMIT ?",
        (account_id, limit),
    )
    return [view(row) for row in rows]


def raw_of(account_id: int, giveout_id: str) -> dict | None:
    """Ответ Ozon по акту как есть — чтобы видеть, что площадка реально прислала."""
    row = db.query_one(
        "SELECT raw FROM ozon_giveouts WHERE account_id = ? AND id = ?", (account_id, giveout_id)
    )
    if not row:
        return None
    try:
        return json.loads(row["raw"] or "{}")
    except ValueError:
        return {}
