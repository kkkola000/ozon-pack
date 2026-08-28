"""Настройки, которые меняются из интерфейса и переживают обновление кода.

Значение из панели (таблица kv) важнее значения из .env: файл при обновлении
намеренно не перезаписывается, поэтому менять поведение через него неудобно.
"""
from __future__ import annotations

import json
import secrets

from . import db
from .config import settings

KV_RETURNS_STATUSES = "returns_ready_statuses"
KV_PRINT_MODE = "print_mode"

# Как печатать стикер:
#   auto  — PDF везде, кроме Safari (он печатает PDF во фрейме пустым листом);
#   pdf   — всегда исходный PDF от Ozon;
#   image — всегда HTML-страница с картинкой стикера.
PRINT_MODES = [
    ("auto", "Автоматически", "PDF, а в Safari — картинка"),
    ("pdf", "Всегда PDF", "исходный файл Ozon"),
    ("image", "Всегда картинкой", "надёжнее в Safari и на старых браузерах"),
]
DEFAULT_PRINT_MODE = "auto"


def get_print_mode() -> str:
    value = (db.kv_get(KV_PRINT_MODE) or "").strip()
    return value if value in {code for code, _l, _h in PRINT_MODES} else DEFAULT_PRINT_MODE


def set_print_mode(mode: str, user: dict | None = None) -> str:
    if mode not in {code for code, _l, _h in PRINT_MODES}:
        raise ValueError(f"Неизвестный режим печати: {mode}")
    db.kv_set(KV_PRINT_MODE, mode)
    db.log_event("print_mode_set", user=user, message=mode)
    return mode

# Статусы возвратов из /v1/returns/list (visual.status.sys_name).
# Забрать со стороны продавца можно только те, что физически лежат в пункте выдачи.
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


# ------------------------------------------------------------------ принтер этикеток
KV_PRINTER = "printer_config"
KV_AGENT_TOKEN = "print_agent_token"


def get_printer_config() -> dict:
    """Настройки принтера этикеток (печать через локального агента)."""
    raw = db.kv_get(KV_PRINTER) or ""
    data = {}
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            data = {}
    return {
        "enabled": bool(data.get("enabled")),
        "host": str(data.get("host") or ""),
        "port": int(data.get("port") or 9100),
        "dpi": int(data.get("dpi") or 203),
        "gap_mm": float(data.get("gap_mm", 2)),
        "gap_offset_mm": float(data.get("gap_offset_mm", 0)),
        "direction": int(data.get("direction", 1)),
        "copies": int(data.get("copies") or 1),
        "invert": bool(data.get("invert")),
        "threshold": int(data.get("threshold") or 160),
    }


def set_printer_config(values: dict, user: dict | None = None) -> dict:
    current = get_printer_config()
    current.update({key: value for key, value in values.items() if key in current})
    current["port"] = max(1, min(65535, int(current["port"])))
    current["dpi"] = 203 if int(current["dpi"]) not in (203, 300) else int(current["dpi"])
    current["copies"] = max(1, min(10, int(current["copies"])))
    current["threshold"] = max(1, min(254, int(current["threshold"])))
    current["direction"] = 1 if int(current["direction"]) else 0
    db.kv_set(KV_PRINTER, json.dumps(current, ensure_ascii=False))
    db.log_event(
        "printer_config_set",
        user=user,
        message=f"{'включена' if current['enabled'] else 'выключена'}, {current['host']}:{current['port']}",
    )
    return current


def get_agent_token(create: bool = True) -> str:
    token = (db.kv_get(KV_AGENT_TOKEN) or "").strip()
    if not token and create:
        token = secrets.token_urlsafe(32)
        db.kv_set(KV_AGENT_TOKEN, token)
    return token


def reset_agent_token(user: dict | None = None) -> str:
    token = secrets.token_urlsafe(32)
    db.kv_set(KV_AGENT_TOKEN, token)
    db.log_event("print_agent_token_reset", level="warn", user=user, message="Ключ агента печати заменён")
    return token
