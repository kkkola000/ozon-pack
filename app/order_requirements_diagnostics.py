"""Сводка требований площадки без передачи кодов и смены статуса заказа.

Запуск из каталога панели:
  .venv/bin/python -m app.order_requirements_diagnostics --market ozon --order НОМЕР
Ключи берутся из существующего кабинета и не попадают в вывод.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

from .core import accounts, db
from .core.config import settings
from .core.version import get_version
from .markets.base import MarketError
from .markets.ozon import client as ozon
from .markets.yandex import client as yandex

OZON_FLAGS = ('is_mandatory_mark_needed', 'is_mandatory_mark_possible', 'is_gtd_needed',
              'is_rnpt_needed', 'is_weight_needed', 'has_imei')
OZON_REQUIREMENTS = ('products_requiring_mandatory_mark', 'products_requiring_gtd',
                     'products_requiring_rnpt', 'products_requiring_imei',
                     'products_requiring_weight', 'products_requiring_country', 'products_requiring_change_country')
OZON_ACTIONS = {'ship', 'cancel', 'ship_async', 'ship_async_in_process', 'ship_async_retry',
                'ship_with_additional_info', 'update_cis', 'label_download', 'label_download_small', 'label_download_big'}
MARK_TYPES = {'mandatory_mark', 'imei', 'CIS', 'KIZ', 'DATA_MATRIX', 'GTIN', 'QR_CODE'}
YANDEX_TYPES = {'CIS', 'CIS_OPTIONAL', 'GTD', 'RNPT'}
TABLES = {'ozon': ('postings', 'posting_number'), 'yandex': ('yandex_orders', 'id')}


def _id(value) -> str:
    return str(value) if re.fullmatch(r'[A-Za-z0-9_-]{1,128}', str(value)) else 'unexpected'


def _enums(values, allowed: set[str]) -> list[str]:
    return sorted({v for v in values or [] if isinstance(v, str) and v in allowed})


def _bool(value) -> bool | None:
    return value if isinstance(value, bool) else None


def _error(exc: Exception) -> dict:
    # Тело/текст ошибки площадки может содержать код маркировки или ключ запроса.
    return {'type': type(exc).__name__, 'http_status': getattr(exc, 'status', None)}


def _local(account_id: int, order_id: str) -> dict:
    row = db.query_one('SELECT id,status,products FROM marking_orders WHERE account_id=? AND order_id=?',
                       (account_id, order_id))
    if not row:
        return {'marking_snapshot_present': False, 'products': []}
    counts = {r['item_key']: r['n'] for r in db.query('SELECT item_key,COUNT(*) AS n FROM marking_codes '
                                                    'WHERE marking_order_id=? AND active=1 GROUP BY item_key', (row['id'],))}
    return {'marking_snapshot_present': True, 'status': row['status'], 'products': [
        {'key': _id(p['key']), 'required': bool(p.get('required')), 'possible': bool(p.get('possible')),
         'stored_code_count': counts.get(p['key'], 0)} for p in json.loads(row['products'])]}


def _ozon_product(product: dict) -> dict:
    exemplars = product.get('exemplars') or []
    types = Counter(m.get('mark_type') if m.get('mark_type') in MARK_TYPES else 'other'
                    for e in exemplars for m in e.get('marks') or [] if m.get('mark'))
    return {'product_id': _id(product.get('product_id')), 'quantity': product.get('quantity'),
            **{key: _bool(product.get(key)) for key in OZON_FLAGS},
            'exemplar_count': len(exemplars), 'mark_types_present': dict(types),
            'gtd_filled_count': sum(bool(e.get('gtd')) for e in exemplars),
            'gtd_absent_declared_count': sum(e.get('is_gtd_absent') is True for e in exemplars),
            'rnpt_filled_count': sum(bool(e.get('rnpt')) for e in exemplars)}


def _ozon(account: dict, order_id: str) -> dict:
    api = ozon.get_client(account)
    raw = api.posting_get(order_id, with_exemplars=True)
    if not raw or str(raw.get('posting_number')) != order_id:
        return {'error': {'type': 'PostingNotConfirmed', 'http_status': None}}
    requirements = raw.get('requirements') or {}
    result = {'remote_status': raw.get('status'), 'remote_substatus': raw.get('substatus'),
              'available_actions': _enums(raw.get('available_actions'), OZON_ACTIONS),
              'posting_requirements': {k: [_id(v) for v in requirements.get(k) or []] for k in OZON_REQUIREMENTS}}
    try:
        # Независимо от requirements/actions: это диагностика фактического ответа экземпляров.
        data = api.exemplars_get(order_id)
        result['exemplars'] = {'posting_number_matches': str(data.get('posting_number')) == order_id,
                               'products': [_ozon_product(p) for p in data.get('products') or []]}
    except (MarketError, ValueError) as exc:
        result['exemplars'] = {'error': _error(exc)}
    return result


def _yandex(account: dict, order_id: str) -> dict:
    raw = yandex.get_client(account).order_details(order_id)
    if str(raw.get('orderId')) != order_id:
        return {'error': {'type': 'OrderNotConfirmed', 'http_status': None}}
    products = []
    for item in raw.get('items') or []:
        instances = item.get('instances') or []
        products.append({'item_id': _id(item.get('id')), 'offer_id': _id(item.get('offerId')),
                         'quantity': item.get('count'),
                         'requiredInstanceTypes': _enums(item.get('requiredInstanceTypes'), YANDEX_TYPES),
                         'instance_count': len(instances),
                         'cis_full_present_count': sum(bool(i.get('cisFull')) for i in instances),
                         'cis_present_count': sum(bool(i.get('cis')) for i in instances),
                         'gtd_filled_count': sum(bool(i.get('gtd')) for i in instances),
                         'rnpt_filled_count': sum(bool(i.get('rnpt')) for i in instances)})
    return {'remote_status': raw.get('status'), 'remote_substatus': raw.get('substatus'),
            'program_type': raw.get('programType'), 'products': products}


def inspect_order(account: dict, order_id: str) -> dict:
    result = {'version': get_version(), 'market': account['marketplace'], 'account_id': account['id'],
              'order_id': order_id, 'local': _local(account['id'], order_id)}
    try:
        result.update((_ozon if account['marketplace'] == 'ozon' else _yandex)(account, order_id))
    except (MarketError, ValueError) as exc:
        result['error'] = _error(exc)
    return result


def _account(market: str, order_id: str, account_id: int | None) -> dict:
    candidates = [a for a in accounts.all_accounts(active_only=True)
                  if a['marketplace'] == market and accounts.is_configured(a)
                  and (account_id is None or a['id'] == account_id)]
    if len(candidates) == 1:
        return candidates[0]
    table, key = TABLES[market]
    found = [a for a in candidates if db.query_one(f'SELECT 1 FROM {table} WHERE account_id=? AND {key}=?',
                                                  (a['id'], order_id))]
    if len(found) == 1:
        return found[0]
    raise ValueError('AccountNotConfirmed')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--market', choices=tuple(TABLES), required=True)
    parser.add_argument('--order', required=True)
    parser.add_argument('--account-id', type=int)
    args = parser.parse_args(argv)
    if _id(args.order) != args.order:
        parser.error('Неверный номер заказа')
    try:
        if not Path(settings.db_path).is_file():
            raise FileNotFoundError('DatabaseNotFound')
        result = inspect_order(_account(args.market, args.order, args.account_id), args.order)
    except (ValueError, OSError, sqlite3.Error) as exc:
        result = {'error': _error(exc), 'hint': 'Запустите из каталога панели; при нескольких кабинетах укажите --account-id.'}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get('error') else 0


if __name__ == '__main__':
    raise SystemExit(main())
