"""Готовность заказа на площадке: скан инициирует переход, чтение подтверждает."""
from __future__ import annotations

from . import db, marking, order_lock

SCHEMA = """
CREATE TABLE IF NOT EXISTS ready_requests (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    order_id TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id,order_id)
);
"""


def attempt(account: dict, order_id: str, write, *, retry_confirmed: bool = False):
    old = db.query_one('SELECT status FROM ready_requests WHERE account_id=? AND order_id=?',
                       (account['id'], order_id))
    if old and old['status'] == 'uncertain' and not retry_confirmed:
        raise ValueError('Площадка пока не подтвердила переход заказа. Повторная отправка не выполняется; повторите скан для проверки.')
    db.execute("INSERT INTO ready_requests VALUES(?,?,'uncertain',?) ON CONFLICT(account_id,order_id) "
               "DO UPDATE SET status='uncertain',updated_at=excluded.updated_at", (account['id'], order_id, db.now_iso()))
    try:
        return write()
    except Exception as exc:
        if getattr(exc, 'status', None) in {400, 401, 403, 404, 409, 422, 429}:
            db.execute("UPDATE ready_requests SET status='rejected',updated_at=? WHERE account_id=? AND order_id=?",
                       (db.now_iso(), account['id'], order_id))
        raise


def confirmed(account: dict, order_id: str):
    db.execute("UPDATE ready_requests SET status='confirmed',updated_at=? WHERE account_id=? AND order_id=?",
               (db.now_iso(), account['id'], order_id))


def call(account: dict, user: dict, order_id: str):
    from ..markets import registry

    workspace = registry.get(account['marketplace']).workspace
    if workspace and workspace.ready:
        result = workspace.ready(account, user, order_id)
        if isinstance(result, dict) and result.get('status') != 'ok':
            raise ValueError(result.get('message') or 'Площадка не подтвердила готовность заказа')
        return result
    raise ValueError('Площадка не поддерживает автоматический переход заказа')


def start(account: dict, user: dict, order_id: str) -> dict:
    """deferred — переход отложен до маркировки; заказ можно открыть для сканов."""
    with order_lock.hold(account['id'], order_id):
        saved = marking.prepare(account, order_id)
        if saved and saved['status'] != 'accepted' and any(p.get('required') or p.get('possible') for p in marking.products(saved)):
            return {'deferred': True, 'result': None}
        result = call(account, user, order_id)
    return {'deferred': False, 'result': result}


def start_and_refresh(account: dict, user: dict, order_id: str):
    from ..markets import registry

    result = start(account, user, order_id)
    board = registry.get(account['marketplace']).orders
    row = db.query_one(f'SELECT * FROM {board.table} WHERE account_id=? AND {board.key}=?', (account['id'], order_id))
    if not row:
        raise ValueError('Площадка изменила номер заказа. Обновите заказы.')
    return result['deferred'], row


def complete_guard(account: dict, user: dict, order_id: str):
    with order_lock.hold(account['id'], order_id):
        saved = marking.prepare(account, order_id)
        marking.guard(account, order_id)
        if marking.needs_validation(saved):
            import json

            outcome = marking.source(account).poll(account, order_id, json.loads(marking._open(saved['context'])),
                                                    marking.codes(saved, full=True))
            if outcome['status'] != 'accepted':
                db.execute("UPDATE marking_orders SET status='uncertain',error=? WHERE id=?",
                           ('Площадка пока не подтвердила актуальные ЧЗ. Завершение заблокировано.', saved['id']))
                raise ValueError('Дождитесь подтверждения ЧЗ площадкой')
        call(account, user, order_id)
