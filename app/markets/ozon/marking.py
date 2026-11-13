"""Контракт ЧЗ Ozon v6/v5: полный набор экземпляров, без выдуманных полей."""
from __future__ import annotations

from copy import deepcopy

from ...core import db, marking, readiness
from ..base import MarkingSource
from . import client, store


def _posting(account: dict, order_id: str) -> tuple[dict, dict, set[str]]:
    raw = client.get_client(account).posting_get(order_id)
    if not raw or str(raw.get('posting_number') or '') != order_id:
        raise ValueError('Ozon не подтвердил отправление. Обновите заказы и повторите скан.')
    if raw.get('status') not in store.WORK_STATUSES:
        label = store.STATUS_LABELS.get(raw.get('status'), 'вне сборки')
        raise ValueError(f'Отправление в Ozon уже в статусе «{label}» и недоступно для сборки.')
    items = store.posting_items(account['id'], order_id)
    local = {str(i['sku']): int(i['quantity']) for i in items}
    actual = {str(p.get('sku') or ''): int(p.get('quantity') or 0) for p in raw.get('products') or []}
    if not local or local != actual:
        raise ValueError('Состав отправления Ozon изменился. Обновите заказы перед сборкой.')
    required = {str(s) for s in (raw.get('requirements') or {}).get('products_requiring_mandatory_mark') or []}
    required.update(str(p['sku']) for p in raw.get('products') or [] if p.get('mandatory_mark'))
    # Не теряем известное обязательство, если Ozon снял флаг после сборки.
    required.update(str(i['sku']) for i in items if i.get('mandatory_mark'))
    with db.write() as conn:
        store.upsert_posting(conn, account['id'], raw)
        if required:
            # Чтение экземпляров ниже может завершиться ошибкой. Не стираем
            # последнее известное требование до успешного сохранения снимка ЧЗ.
            conn.execute('UPDATE postings SET requires_mark=1 WHERE account_id=? AND posting_number=?',
                         (account['id'], order_id))
            conn.executemany('UPDATE posting_items SET mandatory_mark=1 WHERE account_id=? AND posting_number=? AND sku=?',
                             [(account['id'], order_id, sku) for sku in required])
    return raw, local, required


def prepare(account: dict, order_id: str, *, discover: bool = False) -> dict | None:
    _raw, local, required = _posting(account, order_id)
    saved = marking.order(account['id'], order_id)
    known = bool(saved and (marking.codes(saved, active=False) or any(
        p.get('required') or p.get('possible') for p in marking.products(saved))))
    if not required and not known and not discover:
        # Обычному заказу не нужны экземпляры: Ozon может их не отдавать,
        # в том числе когда отправление уже собрано в личном кабинете.
        return None
    return _exemplars(account, order_id, local, required)


def _exemplars(account: dict, order_id: str, local: dict, required: set[str]) -> dict:
    data = client.get_client(account).exemplars_get(order_id)
    if str(data.get('posting_number') or '') != order_id:
        raise ValueError('Ozon не подтвердил номер отправления экземпляров')
    products = []
    for product in data.get('products') or []:
        key = str(product.get('product_id') or '')
        quantity = int(product.get('quantity') or 0)
        exemplars = product.get('exemplars') or []
        if key not in local or quantity != local[key] or len(exemplars) != quantity:
            raise ValueError('Состав экземпляров Ozon не совпадает с заказом. Обновите заказы.')
        if any(not e.get('exemplar_id') for e in exemplars):
            raise ValueError('Ozon не вернул идентификаторы экземпляров')
        needed = bool(product.get('is_mandatory_mark_needed') or key in required)
        products.append({'key': key, 'sku': key, 'quantity': quantity,
                         'required': needed,
                         'possible': needed or bool(product.get('is_mandatory_mark_possible'))})
    if {p['key'] for p in products} != set(local):
        raise ValueError('Ozon вернул неполный состав экземпляров')
    return {'products': products, 'context': data}


def _payload(context: dict, codes: list[dict], *, validate: bool = False) -> list[dict]:
    out = []
    values = {(c['item_key'], c['unit_no']): c['raw'] for c in codes}
    for product in context['products']:
        key = str(product['product_id'])
        exemplars = []
        for unit, source in enumerate(product['exemplars'], 1):
            exemplar = {k: deepcopy(source[k]) for k in ('gtd', 'is_gtd_absent', 'is_rnpt_absent', 'marks', 'rnpt', 'weight')
                        if k in source and source[k] is not None}
            if not validate:
                exemplar['exemplar_id'] = source['exemplar_id']
            else:
                exemplar.pop('is_gtd_absent', None)
                exemplar.pop('is_rnpt_absent', None)
            if (key, unit) in values:
                exemplar['marks'] = [m for m in exemplar.get('marks', []) if m.get('mark_type') != 'mandatory_mark']
                exemplar['marks'].append({'mark': values[key, unit], 'mark_type': 'mandatory_mark'})
            exemplars.append(exemplar)
        out.append({'product_id': product['product_id'], 'exemplars': exemplars})
    return out


def submit(account: dict, order_id: str, context: dict, codes: list[dict], dispatch) -> None:
    api = client.get_client(account)
    fresh = api.exemplars_get(order_id)
    baseline = {(str(p['product_id']), tuple(str(e['exemplar_id']) for e in p['exemplars'])) for p in context['products']}
    actual = {(str(p['product_id']), tuple(str(e['exemplar_id']) for e in p.get('exemplars') or []))
              for p in fresh.get('products') or []}
    if baseline != actual:
        raise marking.Rejected({c['item_key']: 'Состав экземпляров Ozon изменился. Требуется сверка заказа.' for c in codes})
    # Берём свежие ГТД/РНПТ/IMEI/вес, не затирая изменения в кабинете Ozon.
    validation = api.exemplars_validate(order_id, _payload(fresh, codes, validate=True))
    result = validation.get('products') or []
    if not result or {str(p.get('product_id')) for p in result} != {str(p['product_id']) for p in fresh['products']}:
        raise ValueError('Ozon не вернул полный результат валидации')
    errors = {}
    for product in result:
        key = str(product['product_id'])
        invalid = False
        for unit, exemplar in enumerate(product.get('exemplars') or [], 1):
            if exemplar.get('valid') is not True or any(m.get('valid') is not True for m in exemplar.get('marks') or []):
                invalid = True
                errors[f'{key}:{unit}'] = 'Данные экземпляра не соответствуют требованиям Ozon'
        if product.get('valid') is not True and not invalid:
            errors[key] = 'Данные товара не соответствуют требованиям Ozon'
    if errors:
        raise marking.Rejected(errors)
    payload = {'posting_number': order_id, 'products': _payload(fresh, codes)}
    if fresh.get('multi_box_qty') is not None:
        payload['multi_box_qty'] = fresh['multi_box_qty']
    dispatch()
    api.exemplars_set(payload)


def _checks(data, context):
    actual = {}
    errors = {}
    failed = False
    units = {(str(p['product_id']), str(e['exemplar_id'])): unit
             for p in context['products'] for unit, e in enumerate(p['exemplars'], 1)}
    for product in data.get('products') or []:
        key = str(product.get('product_id'))
        for exemplar in product.get('exemplars') or []:
            unit = units.get((key, str(exemplar.get('exemplar_id'))))
            error_key = f'{key}:{unit}' if unit else key
            for field in ('gtd', 'rnpt', 'weight'):
                if exemplar.get(field + '_error_codes'):
                    errors[error_key] = 'Ozon отклонил обязательные данные экземпляра'
            for mark in exemplar.get('marks') or []:
                if mark.get('mark_type') == 'mandatory_mark':
                    actual[key, str(exemplar.get('exemplar_id')), mark.get('mark')] = mark.get('check_status')
                    if mark.get('error_codes'):
                        errors[error_key] = 'Код ЧЗ не принят Ozon'
                    if mark.get('check_status') == 'failed':
                        failed = True
    return actual, errors, failed


def poll(account: dict, order_id: str, context: dict, codes: list[dict]) -> dict:
    data = client.get_client(account).exemplars_status(order_id)
    if str(data.get('posting_number') or '') != order_id:
        return {'status': 'checking'}
    actual, errors, failed = _checks(data, context)
    if errors:
        return {'status': 'error', 'errors': errors}
    if failed:
        return {'status': 'retry', 'message': 'Ozon не смог обработать проверку ЧЗ. Передача будет повторена.'}
    exemplar_ids = {str(p['product_id']): [str(e['exemplar_id']) for e in p['exemplars']] for p in context['products']}
    passed = all(actual.get((c['item_key'], exemplar_ids[c['item_key']][c['unit_no'] - 1], c['raw'])) == 'passed'
                 for c in codes)
    if passed and codes and data.get('status') in {'ship_available', 'update_available'}:
        return {'status': 'accepted'}
    if passed and codes:
        raw = client.get_client(account).posting_get(order_id, with_exemplars=True)
        if raw and raw.get('status') == store.STATUS_AWAITING_DELIVER and _applied(raw, codes):
            return {'status': 'accepted'}
    return {'status': 'checking'}


def _applied(raw: dict, codes: list[dict]) -> bool:
    got = {(str(p.get('sku')), e.get('mandatory_mark')) for p in (raw.get('product_exemplars') or {}).get('products') or []
           for e in p.get('exemplars') or []}
    return all((c['item_key'], c['raw']) in got for c in codes)


def finalize(account: dict, order_id: str, _context: dict, codes: list[dict]) -> None:
    api = client.get_client(account)
    raw = api.posting_get(order_id, with_exemplars=True)
    if not raw:
        raise ValueError('Ozon не подтвердил отправление')
    if raw.get('status') == store.STATUS_AWAITING_DELIVER:
        if not _applied(raw, codes):
            readiness.attempt(account, order_id, lambda: api.exemplars_update(order_id))
            raw = api.posting_get(order_id, with_exemplars=True)
            if not raw or not _applied(raw, codes):
                raise ValueError('Ozon пока не подтвердил сохранение ЧЗ в отправлении')
        readiness.confirmed(account, order_id)
    elif raw.get('status') != store.STATUS_AWAITING_PACKAGING:
        raise ValueError('Отправление Ozon вышло из рабочего статуса')


def redistribute(account, user, order_id, codes):
    from . import cargo
    return cargo.redistribute(account, user, order_id, codes)


SOURCE = MarkingSource(prepare, submit, poll, finalize, lambda item: str(item.get('sku') or ''),
                       discover=lambda account, order_id: prepare(account, order_id, discover=True), redistribute=redistribute)
