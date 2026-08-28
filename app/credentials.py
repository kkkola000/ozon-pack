"""Ключи Ozon Seller API: значения из настроек панели важнее значений из .env.

Ключи, введённые в интерфейсе, лежат в таблице kv и подхватываются без
перезапуска: после сохранения клиент Ozon пересоздаётся.
"""
from __future__ import annotations

import re
from typing import Any

from . import db
from .config import settings

KV_CLIENT_ID = "ozon_client_id"
KV_API_KEY = "ozon_api_key"

# Ключи Ozon — печатаемый ASCII без пробелов. Проверка нужна не для красоты:
# кириллица в заголовке HTTP роняет запрос ещё до обращения к Ozon.
ALLOWED_CHARS = re.compile(r"^[\x21-\x7e]+$")
MAX_LENGTH = 200


def validate(client_id: str, api_key: str) -> str | None:
    """Понятная причина отказа или None, если всё в порядке."""
    if not client_id or not api_key:
        return "Заполните Client-Id и Api-Key"
    for name, value in (("Client-Id", client_id), ("Api-Key", api_key)):
        if len(value) > MAX_LENGTH:
            return f"{name} длиннее {MAX_LENGTH} символов — похоже, скопировалось лишнее"
        if not ALLOWED_CHARS.match(value):
            return (
                f"{name} содержит недопустимые символы (например, кириллицу или пробел). "
                "Скопируйте ключ из личного кабинета Ozon заново."
            )
    return None


def get_credentials() -> tuple[str, str, str]:
    """(client_id, api_key, источник): 'panel', 'env' или 'none'."""
    client_id = (db.kv_get(KV_CLIENT_ID) or "").strip()
    api_key = (db.kv_get(KV_API_KEY) or "").strip()
    if client_id and api_key:
        return client_id, api_key, "panel"
    if settings.ozon_client_id and settings.ozon_api_key:
        return settings.ozon_client_id, settings.ozon_api_key, "env"
    return "", "", "none"


def is_demo() -> bool:
    """Демо-режим: принудительно через OZON_DEMO=1 либо когда ключей нет."""
    if settings.demo_forced:
        return True
    _client_id, _api_key, source = get_credentials()
    return source == "none"


def set_credentials(client_id: str, api_key: str, user: dict | None = None) -> None:
    from .ozon import reset_client

    db.kv_set(KV_CLIENT_ID, client_id.strip())
    db.kv_set(KV_API_KEY, api_key.strip())
    db.log_event(
        "ozon_credentials_set",
        user=user,
        message=f"Ключи Ozon сохранены (Client-Id {client_id.strip()})",
    )
    reset_client()


def clear_credentials(user: dict | None = None) -> None:
    from .ozon import reset_client

    db.kv_set(KV_CLIENT_ID, "")
    db.kv_set(KV_API_KEY, "")
    db.log_event("ozon_credentials_cleared", level="warn", user=user, message="Ключи Ozon удалены из панели")
    reset_client()


def mask(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 4:
        return "•" * len(value)
    return "•" * max(4, len(value) - 4) + value[-4:]


def status() -> dict[str, Any]:
    """Состояние подключения для страницы настроек."""
    client_id, api_key, source = get_credentials()
    return {
        "client_id": client_id,
        "api_key_masked": mask(api_key),
        "source": source,
        "source_label": {
            "panel": "введены в панели",
            "env": "заданы в файле .env",
            "none": "не заданы",
        }[source],
        "demo": is_demo(),
        "demo_forced": settings.demo_forced,
        "base_url": settings.ozon_base_url,
    }
