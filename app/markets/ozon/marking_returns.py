"""Ozon evidence for reuse; no requests to the Honest Sign system."""
from __future__ import annotations

import logging

from ...core import accounts, db, marking, marking_registry
from . import client

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


def try_release(owner: dict) -> bool:
    """Check explicit cancellation only; returns are handled by common act jobs."""
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
        return cancelled(account, raw)
    except (client.OzonError, ValueError, LookupError, KeyError, TypeError, AttributeError):
        log.warning('Не подтверждена отвязка Честного знака Ozon: кабинет %s, отправление %s', account['id'], number)
        return False
