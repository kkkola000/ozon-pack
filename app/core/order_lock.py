"""Короткая межпроцессная аренда операции заказа; сеть — вне SQLite-транзакции."""
from __future__ import annotations

import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

from . import db
from .config import settings

_held: ContextVar[frozenset] = ContextVar('order_operations', default=frozenset())

SCHEMA = """
CREATE TABLE IF NOT EXISTS order_operations (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    order_id TEXT NOT NULL,
    token TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY(account_id, order_id)
);
"""


@contextmanager
def hold(account_id: int, order_id: str):
    identity = (settings.db_path, account_id, order_id)
    if identity in _held.get():
        yield
        return
    token = secrets.token_hex(16)
    now = db.now_iso()
    expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    with db.write() as conn:
        conn.execute('DELETE FROM order_operations WHERE account_id = ? AND order_id = ? AND expires_at < ?',
                     (account_id, order_id, now))
        changed = conn.execute('INSERT OR IGNORE INTO order_operations VALUES(?,?,?,?)',
                               (account_id, order_id, token, expires)).rowcount
    if not changed:
        raise ValueError('Заказ уже обрабатывается. Дождитесь результата и повторите действие.')
    context_token = _held.set(_held.get() | {identity})
    try:
        yield
    finally:
        _held.reset(context_token)
        db.execute('DELETE FROM order_operations WHERE account_id = ? AND order_id = ? AND token = ?',
                   (account_id, order_id, token))
