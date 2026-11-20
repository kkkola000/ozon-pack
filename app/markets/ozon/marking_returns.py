"""Ozon evidence for reuse; no requests to the Honest Sign system."""
from __future__ import annotations

import logging

from ...core import accounts, db, marking, marking_registry
from . import client, store

log = logging.getLogger(__name__)


def cancelled(account: dict, raw: dict) -> bool:
    cancellation = raw.get('cancellation')
    if (raw.get('status') != 'cancelled' or not isinstance(cancellation, dict)
            or cancellation.get('cancelled_after_ship') is not False
            or raw.get('delivering_date') or raw.get('fact_delivery_date')):
        return False
    number = str(raw.get('posting_number') or '')
    saved = marking.order(account['id'], number)
    if not saved:
        return False
    for mark in marking.codes(saved):
        marking_registry.release(saved['archive_id'], mark['id'], reason='ozon_cancelled_before_assembly')
    return True


def _returned(api, number):
    found, last_id = {}, 0
    for _ in range(20):
        rows, more = api.returns_list(limit=500, last_id=last_id, filter_={'posting_numbers': [number]})
        for row in rows:
            if str(row.get('posting_number') or '') == number and row.get('id'):
                found[str(row['id'])] = row
        if not more:
            return list(found.values())
        next_id = int(rows[-1]['id']) if rows else 0
        if next_id <= last_id:
            break
        last_id = next_id
    return []  # Incomplete pagination cannot prove a complete return.


def try_release(owner: dict) -> bool:
    """Recheck the old Ozon shipment when its code is scanned for a new order.

    A received return must also be accepted locally. The return API supplies
    SKU/quantity, not CIS: partial returns of identical goods stay blocked
    until the administrator detaches the specific code.
    """
    account = accounts.get(owner['account_id'])
    if not account or not account['active'] or account['marketplace'] != 'ozon':
        return False
    number = owner['order_id']
    saved = marking.order(account['id'], number)
    if not saved:
        return False
    try:
        api = client.get_client(account)
        raw = api.posting_get(number)
        if not raw or str(raw.get('posting_number') or '') != number:
            return False
        if cancelled(account, raw):
            return True
        if raw.get('status') in store.WORK_STATUSES or saved['status'] in marking_registry.BUSY:
            return False
        returned = {}
        for row in _returned(api, number):
            if (str(row.get('schema') or '').upper() != 'FBS'
                    or (((row.get('visual') or {}).get('status') or {}).get('sys_name')) != 'ReceivedBySeller'):
                continue
            product = row.get('product') or {}
            sku, quantity = str(product.get('sku') or ''), int(product.get('quantity') or 0)
            accepted = db.query_one("SELECT 1 FROM returns WHERE account_id=? AND id=? AND posting_number=? "
                                    "AND sku=? AND quantity=? AND mark='ok' AND received_at IS NOT NULL",
                                    (account['id'], str(row['id']), number, sku, quantity))
            if accepted and quantity > 0:
                returned[sku] = returned.get(sku, 0) + quantity
        actual = {}
        for product in raw.get('products') or []:
            sku, quantity = str(product.get('sku') or ''), int(product.get('quantity') or 0)
            if not sku or sku in actual or quantity < 1:
                return False
            actual[sku] = quantity
        eligible = {p['key'] for p in marking.products(saved)
                    if returned.get(p['sku']) == actual.get(p['sku']) == int(p['quantity'])}
        for mark in marking.codes(saved):
            if mark['item_key'] in eligible:
                marking_registry.release(saved['archive_id'], mark['id'], reason='ozon_return_received')
        return bool(eligible)
    except (client.OzonError, ValueError, LookupError, KeyError, TypeError, AttributeError):
        log.warning('Не подтверждена отвязка Честного знака Ozon: кабинет %s, отправление %s', account['id'], number)
        return False
