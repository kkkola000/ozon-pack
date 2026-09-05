"""Кабинеты: магазины на площадках и ключи доступа к их API.

Кабинет — единица изоляции данных. Все таблицы с заказами, товарами и
возвратами хранят account_id, поэтому отправление одного магазина никогда не
попадёт в сборочное задание другого. Ключи лежат в самом кабинете, а не в .env:
их вводят в настройках панели и меняют без перезапуска.
"""
from __future__ import annotations

import re
from typing import Any

from . import db
from .config import settings

# Площадки и то, как называются их ключи в личных кабинетах.
MARKETPLACES: dict[str, dict[str, str]] = {
    "ozon": {
        "title": "Ozon",
        "id_label": "Client-Id",
        "key_label": "Api-Key",
        "hint": "Личный кабинет Ozon → Настройки → Seller API",
    },
    "avito": {
        "title": "Avito",
        "id_label": "client_id",
        "key_label": "client_secret",
        "hint": "Личный кабинет Avito → Настройки → Профиль → API",
    },
}
DEFAULT_MARKETPLACE = "ozon"

# Ключи площадок — печатаемый ASCII без пробелов. Проверка нужна не для красоты:
# кириллица в заголовке HTTP роняет запрос ещё до обращения к площадке.
ALLOWED_CHARS = re.compile(r"^[\x21-\x7e]+$")
MAX_LENGTH = 200
MAX_TITLE = 60


def marketplace_title(marketplace: str) -> str:
    return MARKETPLACES.get(marketplace, {}).get("title", marketplace or "—")


def validate(marketplace: str, title: str, client_id: str, api_key: str, *, keys_required: bool = False) -> str | None:
    """Понятная причина отказа или None, если всё в порядке."""
    if marketplace not in MARKETPLACES:
        return "Неизвестная площадка"
    if not title.strip():
        return "Укажите название кабинета"
    if len(title.strip()) > MAX_TITLE:
        return f"Название длиннее {MAX_TITLE} символов"
    meta = MARKETPLACES[marketplace]
    if keys_required and not (client_id and api_key):
        return f"Заполните {meta['id_label']} и {meta['key_label']}"
    if bool(client_id) != bool(api_key):
        return f"Нужны оба ключа: {meta['id_label']} и {meta['key_label']}"
    for name, value in ((meta["id_label"], client_id), (meta["key_label"], api_key)):
        if not value:
            continue
        if len(value) > MAX_LENGTH:
            return f"{name} длиннее {MAX_LENGTH} символов — похоже, скопировалось лишнее"
        if not ALLOWED_CHARS.match(value):
            return (
                f"{name} содержит недопустимые символы (например, кириллицу или пробел). "
                "Скопируйте ключ из личного кабинета заново."
            )
    return None


def mask(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 4:
        return "•" * len(value)
    return "•" * max(4, len(value) - 4) + value[-4:]


# ---------------------------------------------------------------- чтение
def _row_to_dict(row: Any) -> dict:
    account = dict(row)
    account["marketplace_title"] = marketplace_title(account["marketplace"])
    account["demo"] = is_demo(account)
    return account


def all_accounts(*, active_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM accounts"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY sort, id"
    return [_row_to_dict(row) for row in db.query(sql)]


def get(account_id: int | str | None) -> dict | None:
    if account_id in (None, ""):
        return None
    try:
        account_id = int(account_id)
    except (TypeError, ValueError):
        return None
    row = db.query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    return _row_to_dict(row) if row else None


def default_account() -> dict | None:
    """Первый активный кабинет; если активных нет — просто первый."""
    row = db.query_one("SELECT * FROM accounts WHERE active = 1 ORDER BY sort, id LIMIT 1")
    if row is None:
        row = db.query_one("SELECT * FROM accounts ORDER BY sort, id LIMIT 1")
    return _row_to_dict(row) if row else None


def resolve(account_id: int | str | None) -> dict | None:
    """Запрошенный кабинет, если он есть и включён, иначе — кабинет по умолчанию."""
    account = get(account_id)
    if account and account["active"]:
        return account
    return default_account()


def credentials(account: dict | None) -> tuple[str, str, str]:
    """(client_id, api_key, источник): 'panel', 'env' или 'none'."""
    if account:
        client_id = (account.get("client_id") or "").strip()
        api_key = (account.get("api_key") or "").strip()
        if client_id and api_key:
            return client_id, api_key, "panel"
        # Ключи из .env — только для кабинета Ozon: другой площадки там нет.
        if account.get("marketplace") == "ozon" and settings.ozon_client_id and settings.ozon_api_key:
            return settings.ozon_client_id, settings.ozon_api_key, "env"
    return "", "", "none"


def is_demo(account: dict | None) -> bool:
    """Демо-режим: принудительно через OZON_DEMO=1 либо когда ключей нет."""
    if settings.demo_forced:
        return True
    if account is None:
        return True
    if account.get("marketplace") == "ozon":
        client_id = (account.get("client_id") or "").strip()
        api_key = (account.get("api_key") or "").strip()
        if client_id and api_key:
            return False
        return not (settings.ozon_client_id and settings.ozon_api_key)
    return not ((account.get("client_id") or "").strip() and (account.get("api_key") or "").strip())


def status(account: dict | None) -> dict:
    """Состояние подключения кабинета для страницы настроек."""
    client_id, api_key, source = credentials(account)
    return {
        "client_id": client_id,
        "api_key_masked": mask(api_key),
        "source": source,
        "source_label": {
            "panel": "введены в панели",
            "env": "заданы в файле .env",
            "none": "не заданы",
        }[source],
        "demo": is_demo(account),
        "demo_forced": settings.demo_forced,
    }


# ---------------------------------------------------------------- запись
def create(marketplace: str, title: str, client_id: str = "", api_key: str = "", *, user: dict | None = None) -> int:
    row = db.query_one("SELECT COALESCE(MAX(sort), -1) + 1 AS next FROM accounts")
    with db.write() as conn:
        cur = conn.execute(
            "INSERT INTO accounts(marketplace, title, client_id, api_key, active, sort, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, 1, ?, ?, ?)",
            (marketplace, title.strip(), client_id.strip(), api_key.strip(), row["next"], db.now_iso(), db.now_iso()),
        )
        account_id = int(cur.lastrowid)
        db.log_event(
            "account_created",
            account_id=account_id,
            user=user,
            message=f"Добавлен кабинет «{title.strip()}» ({marketplace_title(marketplace)})",
            conn=conn,
        )
    _reset_clients(account_id)
    return account_id


def update(account_id: int, *, title: str | None = None, client_id: str | None = None,
           api_key: str | None = None, active: bool | None = None, user: dict | None = None) -> None:
    sets: list[str] = []
    params: list[Any] = []
    if title is not None:
        sets.append("title = ?")
        params.append(title.strip())
    if client_id is not None:
        sets.append("client_id = ?")
        params.append(client_id.strip())
    if api_key is not None:
        sets.append("api_key = ?")
        params.append(api_key.strip())
    if active is not None:
        sets.append("active = ?")
        params.append(1 if active else 0)
    if not sets:
        return
    sets.append("updated_at = ?")
    params.extend([db.now_iso(), account_id])
    db.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id = ?", params)
    if client_id is not None or api_key is not None:
        account = get(account_id)
        db.log_event(
            "account_credentials_set",
            account_id=account_id,
            user=user,
            # В журнал попадает только идентификатор: секрет там не нужен.
            message=f"Ключи кабинета «{(account or {}).get('title', account_id)}» сохранены",
        )
    _reset_clients(account_id)


def delete(account_id: int, *, user: dict | None = None) -> None:
    """Удалить кабинет вместе с его заказами, товарами и возвратами."""
    account = get(account_id)
    with db.write() as conn:
        for table in ("postings", "posting_items", "products", "product_barcodes", "returns",
                      "avito_orders", "avito_order_items"):
            conn.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM pack_state WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        db.log_event(
            "account_deleted",
            level="warn",
            account_id=account_id,
            user=user,
            message=f"Удалён кабинет «{(account or {}).get('title', account_id)}» вместе с данными",
            conn=conn,
        )
    _reset_clients(account_id)


def _reset_clients(account_id: int | None = None) -> None:
    from . import avito, ozon

    ozon.reset_client(account_id)
    avito.reset_client(account_id)
