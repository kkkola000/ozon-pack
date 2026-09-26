"""Кабинеты: магазины на площадках и ключи доступа к их API.

Кабинет — единица изоляции данных. Все таблицы с заказами, товарами и
возвратами хранят account_id, поэтому отправление одного магазина никогда не
попадёт в сборочное задание другого. Ключи лежат в самом кабинете, а не в .env:
их вводят в настройках панели и меняют без перезапуска.
"""
from __future__ import annotations

import re
from typing import Any

from . import crypto, db

# Ключи площадок — печатаемый ASCII без пробелов. Проверка нужна не для красоты:
# кириллица в заголовке HTTP роняет запрос ещё до обращения к площадке.
ALLOWED_CHARS = re.compile(r"^[\x21-\x7e]+$")
MAX_LENGTH = 200
MAX_TITLE = 60


def _registry():
    """Реестр площадок — лениво: он импортирует пакеты площадок, а те — ядро."""
    from ..markets import registry

    return registry


def marketplace_title(marketplace: str) -> str:
    market = _registry().get(marketplace)
    return market.title if market else (marketplace or "—")


def validate(marketplace: str, title: str, client_id: str, api_key: str, *, keys_required: bool = False) -> str | None:
    """Понятная причина отказа или None, если всё в порядке."""
    meta = _registry().get(marketplace)
    if meta is None:
        return "Неизвестная площадка"
    if not title.strip():
        return "Укажите название кабинета"
    if len(title.strip()) > MAX_TITLE:
        return f"Название длиннее {MAX_TITLE} символов"
    if keys_required and not (client_id and api_key):
        return f"Заполните {meta.id_label} и {meta.key_label}"
    if bool(client_id) != bool(api_key):
        return f"Нужны оба ключа: {meta.id_label} и {meta.key_label}"
    for name, value in ((meta.id_label, client_id), (meta.key_label, api_key)):
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
    account["configured"] = is_configured(account)
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


def credentials(account: dict | None) -> tuple[str, str, str]:
    """(client_id, api_key, источник): 'panel', 'env' или 'none'.

    Секретная половина ключей хранится в базе зашифрованной (app/crypto.py),
    здесь она расшифровывается — это единственная точка, где ключи достают.
    """
    if account:
        client_id = (account.get("client_id") or "").strip()
        api_key = crypto.decrypt(account.get("api_key"))
        if client_id and api_key:
            return client_id, api_key, "panel"
        # Запасные ключи из .env есть только у площадок, которые их объявили (Ozon).
        market = _registry().get(account.get("marketplace"))
        if market and market.env_credentials:
            env_id, env_key = market.env_credentials()
            if env_id and env_key:
                return env_id, env_key, "env"
    return "", "", "none"


def is_configured(account: dict | None) -> bool:
    """Есть ли у кабинета ключи. Без них панель не показывает ничего."""
    if account is None:
        return False
    client_id, api_key, _source = credentials(account)
    return bool(client_id and api_key)


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
        "configured": bool(client_id and api_key),
    }


# ---------------------------------------------------------------- запись
def create(marketplace: str, title: str, client_id: str = "", api_key: str = "", *, user: dict | None = None) -> int:
    row = db.query_one("SELECT COALESCE(MAX(sort), -1) + 1 AS next FROM accounts")
    with db.write() as conn:
        cur = conn.execute(
            "INSERT INTO accounts(marketplace, title, client_id, api_key, active, sort, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, 1, ?, ?, ?)",
            (marketplace, title.strip(), client_id.strip(), crypto.encrypt(api_key),
             row["next"], db.now_iso(), db.now_iso()),
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
        params.append(crypto.encrypt(api_key))
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
    market = _registry().get((account or {}).get("marketplace"))
    # Таблицы площадки объявляет она сама; у кабинета неизвестной площадки
    # (запись из старой версии) чистим таблицы всех — лишнее ничего не найдёт.
    tables = market.tables if market else tuple(t for m in _registry().all_markets() for t in m.tables)
    # Плюс общие таблицы кабинета: каталог, штрихкоды и наборы. Они не
    # принадлежат площадке — наполнить каталог умеет и Ozon, и Маркет, — но
    # уходят вместе с кабинетом: чужих данных в панели остаться не должно.
    tables += db.CORE_DATA_TABLES
    with db.write() as conn:
        for table in tables:
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
    for market in _registry().all_markets():
        market.reset_client(account_id)
