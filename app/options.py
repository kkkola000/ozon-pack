"""Настройки, которые меняются из интерфейса и переживают обновление кода.

Значение из панели (таблица kv) важнее значения из .env: файл при обновлении
намеренно не перезаписывается, поэтому менять поведение через него неудобно.
"""
from __future__ import annotations

from . import db
from .config import settings

KV_RETURNS_STATUSES = "returns_ready_statuses"
# Статусы, в которых возврат считается полученным: он уже у нас, и по нему надо
# принять решение. Это отдельная настройка, а не часть списка «готов к выдаче»:
# один список решал бы сразу две задачи — что показывать сборщику к поездке и
# что закрывать актом, — и включить второе без первого было бы нельзя.
KV_RETURNS_RECEIVED = "returns_received_statuses"

RETURN_STATUS_CHOICES = [
    ("ArrivedAtReturnPlace", "В пункте выдачи", "возврат лежит в пункте — его можно забрать"),
    ("WaitingShipment", "Ожидает отгрузки", "готовится к отправке"),
    ("MovingToSeller", "Едет к продавцу", "в пути, забрать нельзя"),
    ("ReturningByCourier", "Везёт курьер", "в пути, забрать нельзя"),
    ("ReceivedBySeller", "Получен продавцом", "уже у вас"),
    ("MovingToOzon", "Едет на склад Ozon", "уезжает на склад Ozon"),
    ("ReturnedToOzon", "На складе Ozon", "хранится у Ozon"),
]
DEFAULT_RETURNS_STATUSES = ["ArrivedAtReturnPlace"]
DEFAULT_RECEIVED_STATUSES = ["ReceivedBySeller"]

# Значения, которые писал в .env установщик прежних версий. Это не осознанный
# выбор пользователя, а устаревшая настройка по умолчанию: файл при обновлении
# не перезаписывается, поэтому старое значение переопределяем на актуальное.
LEGACY_DEFAULTS = {
    ("ArrivedAtReturnPlace", "WaitingShipment"),
    ("ReturnedToSeller", "ReadyForShipment", "WaitingForSeller", "ready_for_shipment", "returned_to_seller"),
}


def get_returns_statuses() -> list[str]:
    raw = (db.kv_get(KV_RETURNS_STATUSES) or "").strip()
    if raw:
        return [item.strip() for item in raw.split(",") if item.strip()]
    from_env = list(settings.returns_ready_statuses)
    if from_env and tuple(from_env) not in LEGACY_DEFAULTS:
        return from_env
    return list(DEFAULT_RETURNS_STATUSES)


def set_returns_statuses(statuses: list[str], user: dict | None = None) -> list[str]:
    cleaned = [s.strip() for s in statuses if s and s.strip()]
    if not cleaned:
        cleaned = list(DEFAULT_RETURNS_STATUSES)
    db.kv_set(KV_RETURNS_STATUSES, ",".join(cleaned))
    db.log_event("returns_statuses_set", user=user, message=", ".join(cleaned))
    return cleaned


def returns_source() -> str:
    return "panel" if (db.kv_get(KV_RETURNS_STATUSES) or "").strip() else "env"


def get_received_statuses() -> list[str]:
    """Статусы, в которых возврат считается полученным.

    Пустое значение — осознанный выбор «акты не вести», поэтому отличаем
    «не задано» (берём умолчание) от «задано пустым».
    """
    raw = db.kv_get(KV_RETURNS_RECEIVED)
    if raw is None:
        return list(DEFAULT_RECEIVED_STATUSES)
    return [item.strip() for item in raw.split(",") if item.strip()]


def set_received_statuses(statuses: list[str], user: dict | None = None) -> list[str]:
    cleaned = [s.strip() for s in statuses if s and s.strip()]
    db.kv_set(KV_RETURNS_RECEIVED, ",".join(cleaned))
    db.log_event("returns_received_set", user=user, message=", ".join(cleaned) or "выключено")
    return cleaned


def wanted_statuses() -> list[str]:
    """Что вообще забирать из Ozon: и к выдаче, и полученное.

    Полученные возвраты надо не только загрузить, но и удержать в базе: чистка
    удаляет записи в незапрошенных статусах, и без этого списка возврат исчез
    бы ровно в тот момент, когда по нему нужно поставить отметку.
    """
    seen: list[str] = []
    for code in list(get_returns_statuses()) + list(get_received_statuses()):
        if code not in seen:
            seen.append(code)
    return seen


def status_label(sys_name: str) -> str:
    for code, label, _hint in RETURN_STATUS_CHOICES:
        if code == sys_name:
            return label
    return sys_name
