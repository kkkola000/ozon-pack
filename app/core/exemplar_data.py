"""Separate per-unit additional information, carried by the durable exemplar queue.

The browser never chooses the account/order. Values are encrypted alongside the
marking snapshot; neither API errors nor logs contain submitted identifiers.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from . import db, pack_state

LABELS = {'gtd': 'Указать ГТД', 'rnpt': 'Указать РНПТ',
          'imei': 'Указать IMEI', 'weight': 'Указать вес, г'}


def values(saved: dict | None) -> dict:
    from . import marking
    stored = json.loads(marking._open(saved['details'])) if saved and saved.get('details') else {}
    supported = {}
    for key, units in stored.items():
        for unit in units:
            fields = at(stored, key, unit)
            if fields:
                supported.setdefault(key, {})[unit] = fields
    return supported


def at(details: dict, key: str, unit: int) -> dict:
    # Old encrypted snapshots can contain fields no longer collected by the panel.
    return {f: v for f, v in details.get(str(key), {}).get(str(unit), {}).items() if f in LABELS}


def required(saved: dict | None) -> bool:
    from . import marking
    return bool(saved and any(p.get('fields') for p in marking.products(saved)))


def ready(saved: dict) -> bool:
    from . import marking
    present = {(m['item_key'], m['unit_no']) for m in marking.codes(saved) if m['status'] != 'error'}
    details = values(saved)
    return all((not p.get('required') or (p['key'], u) in present) and
               all(at(details, p['key'], u).get(f) for f in p.get('fields', []))
               for p in marking.products(saved) for u in range(1, int(p['quantity']) + 1))


def context(saved: dict) -> dict:
    from . import marking
    result = json.loads(marking._open(saved['context']))
    result['_panel_details'] = values(saved)
    return result


def queue(saved: dict, user_id: int | None = None) -> None:
    from . import marking
    if saved['status'] in {'collecting', 'error'} and marking.needs_validation(saved) and ready(saved):
        db.execute("UPDATE marking_orders SET status='queued',checked_at=NULL,user_id=COALESCE(?,user_id) WHERE id=?",
                   (user_id, saved['id']))


def public(saved: dict, key: str) -> dict | None:
    from . import marking
    product = next((p for p in marking.products(saved) if p['key'] == key), None)
    if not product or not product.get('fields'):
        return None
    details = values(saved)
    errors = json.loads(saved.get('field_errors') or '{}')
    return {'key': key, 'revision': saved['revision'], 'status': saved['status'],
            'units': [{'unit': u, 'fields': [
                {'name': f, 'label': LABELS[f], 'value': at(details, key, u).get(f, ''),
                 'error': errors.get(f'data:{key}:{u}:{f}') or errors.get(f'data:{key}:{u}')}
                for f in product['fields']]} for u in range(1, int(product['quantity']) + 1)]}


def _clean(field: str, value, product: dict):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ValueError('Заполните обязательные данные товара')
    value = str(value).strip()
    if not value or len(value) > 150 or any(ord(c) < 32 for c in value):
        raise ValueError(f'Заполните поле «{LABELS[field]}»')
    if field == 'weight':
        try:
            number = Decimal(value.replace(',', '.'))
            if not number.is_finite() or number <= 0 or number != number.to_integral_value():
                raise ValueError('Вес указывается целым положительным числом в граммах')
            if product.get('weight_min') and number < Decimal(str(product['weight_min'])) or (
                product.get('weight_max') and number > Decimal(str(product['weight_max']))):
                raise ValueError('Вес вне диапазона, разрешённого площадкой')
            return int(number)
        except InvalidOperation as exc:
            raise ValueError('Укажите вес в граммах') from exc
    if field == 'imei' and (not value.isascii() or not value.isdigit() or len(value) != 15):
        raise ValueError('IMEI должен содержать 15 цифр')
    return value


def save(account: dict, user: dict, order_id: str, payload: dict) -> None:
    """Called under the same user/order locks as scanning and cancellation."""
    from . import marking, order_lock
    with order_lock.hold(account['id'], order_id):
        saved = marking.order(account['id'], order_id)
        if not saved or type(payload.get('revision')) is not int or payload['revision'] != saved['revision']:
            raise ValueError('Данные заказа изменились. Обновите окно и повторите ввод.')
        if saved['status'] in {'queued', 'checking', 'uncertain'}:
            raise ValueError('Дождитесь результата уже отправленных данных')
        rows = payload.get('items')
        if not isinstance(rows, list) or len(rows) > 500:
            raise ValueError('Некорректные данные экземпляров')
        details = _merge_rows(saved, rows)
        with db.write() as conn:
            current = conn.execute('SELECT account_id,posting_number FROM pack_state WHERE user_id=?', (user['id'],)).fetchone()
            if not current or (current['account_id'], current['posting_number']) != (account['id'], order_id):
                raise ValueError('Открытая сборка изменилась. Повторите ввод в актуальном заказе.')
            pack_state.claim(conn, account, user, order_id)
            conn.execute("UPDATE marking_orders SET details=?,field_errors='{}',revision=revision+1,status='collecting',"
                         'error=NULL,user_id=? WHERE id=?',
                         (marking._seal(json.dumps(details, ensure_ascii=False)), user['id'], saved['id']))
            db.log_event('exemplar_data_saved', account_id=account['id'], user=user, posting_number=order_id,
                         message='Сохранены обязательные данные экземпляров', conn=conn)
        queue(marking.order(account['id'], order_id), user['id'])
        if marking._worker:
            marking._worker.wake.set()


def _merge_rows(saved, rows):
    from . import marking
    by_key = {p['key']: p for p in marking.products(saved)}
    details = values(saved)
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Некорректные данные экземпляров')
        key, unit = str(row.get('key') or ''), row.get('unit')
        product = by_key.get(key)
        if not product or type(unit) is not int or not 1 <= unit <= int(product['quantity']) or (key, unit) in seen:
            raise ValueError('Данные не соответствуют товарам текущего заказа')
        fields = row.get('fields')
        if not isinstance(fields, dict) or set(fields) != set(product.get('fields', [])):
            raise ValueError('Заполните только требуемые поля товара')
        details.setdefault(key, {})[str(unit)] = {f: _clean(f, v, product) for f, v in fields.items()}
        seen.add((key, unit))
    if not seen:
        raise ValueError('Нет данных для передачи')
    return details
