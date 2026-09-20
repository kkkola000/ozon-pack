"""Преобразование ответов Ozon в строки БД и обратно в объекты для UI."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

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


# ------------------------------------------------------------------ заказы Avito
def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
