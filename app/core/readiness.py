"""Готовность заказа на площадке: скан инициирует переход, чтение подтверждает."""
from __future__ import annotations

from . import db, exemplar_data, marking, order_lock

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
    market = registry.get(account['marketplace'])
    if market.cargo:
        market.cargo.before_ready(account, user, order_id)
    if workspace and workspace.ready:
        result = workspace.ready(account, user, order_id)
        if isinstance(result, dict) and result.get('status') != 'ok':
            raise ValueError(result.get('message') or 'Площадка не подтвердила готовность заказа')
        return result
    raise ValueError('Площадка не поддерживает автоматический переход заказа')


def start(account: dict, user: dict, order_id: str) -> dict:
    """deferred — переход отложен до маркировки; заказ можно открыть для сканов."""
    with order_lock.hold(account['id'], order_id):
        from . import cargo
        if cargo.split_unknown(account['id'], order_id):
            saved = marking.order(account['id'], order_id)
            if marking.needs_validation(saved):
                raise ValueError('Дождитесь сверки кодов «Честный знак» разделённого заказа Ozon')
            return {'deferred': False, 'result': call(account, user, order_id)}
        if cargo.split_pending(account['id'], order_id):
            raise ValueError('Дождитесь подтверждения Честный знак новых отправлений Ozon')
        saved = marking.prepare(account, order_id)
        if saved:
            exemplar_data.queue(saved, user['id'])
        from ..markets import registry
        market = registry.get(account['marketplace'])
        if market.cargo:
            market.cargo.prepare_ready(account, user, order_id)
        if saved and saved['status'] != 'accepted' and (marking.needs_validation(saved) or any(
                p.get('possible') for p in marking.products(saved))):
            return {'deferred': True, 'result': None}
        result = call(account, user, order_id)
    return {'deferred': False, 'result': result}


def start_and_refresh(account: dict, user: dict, order_id: str, *, preferred_sku: str | None = None):
    from ..markets import registry

    result = start(account, user, order_id)
    market = registry.get(account['marketplace'])
    board = market.orders
    numbers = (result.get('result') or {}).get('postings') or []
    if numbers and (len(numbers) > 1 or numbers[0] != order_id):
        matching = [n for n in numbers if not preferred_sku or any(
            str(i.get('sku')) == str(preferred_sku) for i in market.labels.items(account['id'], n))]
        if not matching:
            raise ValueError('Площадка не подтвердила отсканированный товар в новых отправлениях')
        order_id = matching[0]
    row = db.query_one(f'SELECT * FROM {board.table} WHERE account_id=? AND {board.key}=?', (account['id'], order_id))
    if not row:
        raise ValueError('Площадка изменила номер заказа. Обновите заказы.')
    return result['deferred'], row


def complete_guard(account: dict, user: dict, order_id: str):
    with order_lock.hold(account['id'], order_id):
        from . import cargo
        if cargo.split_pending(account['id'], order_id):
            raise ValueError('Дождитесь подтверждения Честный знак новых отправлений Ozon')
        cargo.complete_guard(account, order_id)
        saved = marking.prepare(account, order_id)
        if marking.local_only(account, saved):
            provider = marking.source(account)
            if provider.local_complete:
                provider.local_complete(account, order_id, exemplar_data.context(saved), marking.codes(saved, full=True))
                return
        marking.guard(account, order_id)
        context, codes = None, []
        if marking.needs_validation(saved):
            context = exemplar_data.context(saved)
            codes = marking.codes(saved, full=True)
            marking.confirm(account, saved, context, codes)
        call(account, user, order_id)
        if context is not None:
            marking.confirm(account, saved, context, codes, poll=False)
