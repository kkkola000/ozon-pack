"""Настройки, которые меняются из интерфейса и переживают обновление кода.

Значение из панели (таблица kv) важнее значения из .env: файл при обновлении
намеренно не перезаписывается, поэтому менять поведение через него неудобно.
"""
from __future__ import annotations

from . import db
from .config import settings

KV_RETURNS_STATUSES = "returns_ready_statuses"
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


def status_label(sys_name: str) -> str:
    for code, label, _hint in RETURN_STATUS_CHOICES:
        if code == sys_name:
            return label
    return sys_name
