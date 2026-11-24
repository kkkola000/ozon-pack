"""Контракт ЧЗ Ozon v6/v5: полный набор экземпляров, без выдуманных полей."""
from __future__ import annotations

from copy import deepcopy

from ...core import db, exemplar_data, marking, marking_codes, readiness
from ..base import MarkingSource
from . import client, store


def _remember_required(conn, account_id: int, order_id: str, required: set[str]):
    if not required:
        return
    conn.execute('UPDATE postings SET requires_mark=1 WHERE account_id=? AND posting_number=?', (account_id, order_id))
    conn.executemany('UPDATE posting_items SET mandatory_mark=1 WHERE account_id=? AND posting_number=? AND sku=?',
                     [(account_id, order_id, sku) for sku in required])


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
        message = 'Состав отправления Ozon изменился. Обновите заказы перед сборкой.'
        raise marking.Rejected({key: message for key in local}, message)
    required = {str(s) for s in (raw.get('requirements') or {}).get('products_requiring_mandatory_mark') or []}
    required.update(str(p['sku']) for p in raw.get('products') or [] if p.get('mandatory_mark'))
    # Не теряем известное обязательство, если Ozon снял флаг после сборки.
    required.update(str(i['sku']) for i in items if i.get('mandatory_mark'))
    saved = marking.order(account['id'], order_id)
    if saved:
        required.update(p['key'] for p in marking.products(saved) if p.get('required'))
    with db.write() as conn:
        store.upsert_posting(conn, account['id'], raw)
        # Чтение экземпляров ниже может завершиться ошибкой: известное требование сохраняем.
        _remember_required(conn, account['id'], order_id, required)
    return raw, local, required


def prepare(account: dict, order_id: str, *, discover: bool = False) -> dict | None:
    raw, local, required = _posting(account, order_id)
    saved = marking.order(account['id'], order_id)
    known = bool(saved and (marking.needs_validation(saved) or marking.codes(saved, active=False) or any(
        p.get('required') or p.get('possible') for p in marking.products(saved))))
    # Всегда читаем требования отправления, включая уже готовые заказы.
    # Отсутствие старых requirements/actions не доказывает отсутствие ЧЗ/ГТД.
    data = _exemplars(account, order_id, local, required, requirements=raw.get('requirements') or {})
    if saved:
        _reconcile(account, saved, data)
    if any((raw.get('requirements') or {}).get(k) for k in ('products_requiring_country', 'products_requiring_change_country')):
        raise ValueError('Ozon требует указать страну происхождения. Укажите её в кабинете Ozon и обновите заказ.')
    if not known and not discover and not any(p['required'] or p.get('fields') for p in data['products']):
        return None
    return data


def _remote_marks(context: dict, *, all_types: bool = False) -> set:
    return {(str(p['product_id']), marking_codes.comparison_key(m.get('mark')))
            for p in context['products'] for e in p['exemplars'] for m in e.get('marks') or []
            if (all_types or m.get('mark_type') == 'mandatory_mark') and m.get('mark')}


def _reconcile(account: dict, saved: dict, data: dict) -> None:
    if saved['status'] in {'checking', 'uncertain'}:
        return  # Absence during an asynchronous write is not proof of deletion.
    # A changed API type name is not evidence that the full code disappeared.
    remote = _remote_marks(data['context'], all_types=True)
    removed = [m['id'] for m in marking.codes(saved, full=True) if m['status'] == 'accepted'
               and (m['item_key'], marking_codes.comparison_key(m['raw'])) not in remote]
    if not removed:
        return
    def slots(context):
        return {(str(p['product_id']), tuple(str(e['exemplar_id']) for e in p['exemplars'])) for p in context['products']}
    import json
    changed = slots(json.loads(marking._open(saved['context']))) != slots(data['context'])
    if changed and any(int(p['quantity']) > 1 for p in data['products']):
        raise ValueError('Ozon изменил экземпляры товара. Сверьте привязку маркировки перед продолжением.')
    marking.retire(account, saved, removed, data['context'])


def _fields(product, key, requirements):
    fields = [f for f, flag in (('gtd', 'is_gtd_needed'), ('rnpt', 'is_rnpt_needed'),
                                ('weight', 'is_weight_needed')) if product.get(flag)]
    for f, suffix in (('gtd', 'gtd'), ('rnpt', 'rnpt'), ('imei', 'imei'), ('weight', 'weight')):
        if key in {str(s) for s in (requirements or {}).get('products_requiring_' + suffix) or []} and f not in fields:
            fields.append(f)
    return fields


def _exemplars(account: dict, order_id: str, local: dict, required: set[str], *, requirements=None) -> dict:
    try:
        data = client.get_client(account).exemplars_get(order_id)
    except client.OzonError as exc:
        status = getattr(exc, 'status', None)
        hint = 'Проверьте права ключа на этот метод.' if status in {401, 403} else 'Повторите скан для проверки.'
        raise ValueError('Не удалось получить требования к кодам «Честный знак» и ГТД в Ozon '
                         f'(create-or-get, HTTP {status or "неизвестен"}). {hint}') from exc
    if str(data.get('posting_number') or '') != order_id:
        raise ValueError('Ozon не подтвердил номер отправления экземпляров')
    saved = marking.order(account['id'], order_id)
    local_marks = {m['item_key'] for m in marking.codes(saved)} if saved else set()
    products = []
    for product in data.get('products') or []:
        key = str(product.get('product_id') or '')
        quantity = int(product.get('quantity') or 0)
        exemplars = product.get('exemplars') or []
        if key not in local or quantity != local[key] or len(exemplars) != quantity:
            raise ValueError('Состав экземпляров Ozon не совпадает с заказом. Обновите заказы.')
        if any(not e.get('exemplar_id') for e in exemplars):
            raise ValueError('Ozon не вернул идентификаторы экземпляров')
        has_mark = any(m.get('mark_type') == 'mandatory_mark' and m.get('mark')
                       for e in exemplars for m in e.get('marks') or [])
        # Уже отправленный нами необязательный ЧЗ не меняет политику всего товара.
        needed = bool(product.get('is_mandatory_mark_needed') or product.get('is_mandatory_mark_possible')
                      or key in required or has_mark and key not in local_marks)
        fields = _fields(product, key, requirements)
        products.append({'key': key, 'sku': key, 'quantity': quantity,
                         'required': needed,
                         'possible': needed or bool(product.get('is_mandatory_mark_possible')), 'fields': fields,
                         'weight_min': product.get('weight_min'), 'weight_max': product.get('weight_max')})
    if {p['key'] for p in products} != set(local):
        raise ValueError('Ozon вернул неполный состав экземпляров')
    with db.write() as conn:
        _remember_required(conn, account['id'], order_id, {p['key'] for p in products if p['required']})
    return {'products': products, 'context': data, 'details': _seed_details(data, products)}


def _seed_details(data, products):
    details = {}
    for product in data['products']:
        key = str(product['product_id'])
        fields = next(p['fields'] for p in products if p['key'] == key)
        for unit, e in enumerate(product['exemplars'], 1):
            values = {f: e[f] for f in fields if f in e and e[f] and not e.get('is_' + f + '_absent')}
            found = [m['mark'] for m in e.get('marks') or [] if m.get('mark_type') == 'imei' and m.get('mark')]
            if 'imei' in fields and len(found) == 1:
                values['imei'] = found[0]
            if values:
                details.setdefault(key, {})[str(unit)] = values
    return details


def _apply_fields(exemplar: dict, fields: dict, *, validate: bool):
    for field, value in fields.items():
        if field in {'gtd', 'rnpt', 'weight'}:
            if value is None:
                exemplar.pop(field, None)
            else:
                exemplar[field] = value
            if field != 'weight' and not validate:
                exemplar['is_' + field + '_absent'] = value is None
        elif field == 'imei':
            exemplar['marks'] = [m for m in exemplar.get('marks', []) if m.get('mark_type') != 'imei']
            exemplar['marks'].append({'mark': value, 'mark_type': 'imei'})


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
            _apply_fields(exemplar, exemplar_data.at(context.get('_panel_details', {}), key, unit), validate=validate)
            if (key, unit) in values:
                exemplar['marks'] = [m for m in exemplar.get('marks', []) if m.get('mark_type') != 'mandatory_mark']
                exemplar['marks'].append({'mark': values[key, unit], 'mark_type': 'mandatory_mark'})
            exemplars.append(exemplar)
        out.append({'product_id': product['product_id'], 'exemplars': exemplars})
    return out


def _validation_complete(result: list[dict], products: list[dict]) -> bool:
    counts = {str(p['product_id']): len(p['exemplars']) for p in products}
    # An explicit product rejection may lack exemplars; keep its field error.
    # A successful validation must cover every unit before any write is sent.
    return (len(result) == len(counts) and {str(p.get('product_id')) for p in result} == set(counts)
            and all(p.get('valid') is not True or len(p.get('exemplars') or []) == counts[str(p['product_id'])]
                    for p in result))


def submit(account: dict, order_id: str, context: dict, codes: list[dict], dispatch) -> None:
    api = client.get_client(account)
    fresh = api.exemplars_get(order_id)
    baseline = {(str(p['product_id']), tuple(str(e['exemplar_id']) for e in p['exemplars'])) for p in context['products']}
    actual = {(str(p['product_id']), tuple(str(e['exemplar_id']) for e in p.get('exemplars') or []))
              for p in fresh.get('products') or []}
    if baseline != actual:
        raise marking.Rejected({c['item_key']: 'Состав экземпляров Ozon изменился. Требуется сверка заказа.' for c in codes})
    # Берём свежие ГТД/РНПТ/IMEI/вес, не затирая изменения в кабинете Ozon.
    fresh['_panel_details'] = context.get('_panel_details', {})
    validation = api.exemplars_validate(order_id, _payload(fresh, codes, validate=True))
    result = validation.get('products') or []
    if not result or not _validation_complete(result, fresh['products']):
        raise ValueError('Ozon не вернул полный результат валидации')
    errors = {}
    for product in result:
        key = str(product['product_id'])
        invalid = False
        for unit, exemplar in enumerate(product.get('exemplars') or [], 1):
            if exemplar.get('valid') is not True or any(m.get('valid') is not True for m in exemplar.get('marks') or []):
                invalid = True
                invalid_cz = any(m.get('mark_type') == 'mandatory_mark' and m.get('valid') is not True
                                 for m in exemplar.get('marks') or [])
                error_key = f'{key}:{unit}' if invalid_cz or not context.get('_panel_details') else f'data:{key}:{unit}'
                errors[error_key] = 'Данные экземпляра не соответствуют требованиям Ozon'
        if product.get('valid') is not True and not invalid:
            errors[f'data:{key}:1' if context.get('_panel_details', {}).get(key) else key] = 'Данные товара не соответствуют требованиям Ozon'
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
                status = str(exemplar.get(field + '_check_status') or '').lower()
                if exemplar.get(field + '_error_codes') or status in {'error', 'invalid'}:
                    errors[f'data:{key}:{unit}:{field}'] = 'Ozon отклонил данные поля'
                # A failed GTD check is not proof that saved CIS were rejected.
                # Continue reading status instead of dispatching all marks again.
                failed = failed or field != 'gtd' and status == 'failed'
            for mark in exemplar.get('marks') or []:
                if mark.get('mark_type') not in {'mandatory_mark', 'imei'}:
                    continue
                status = str(mark.get('check_status') or '').lower()
                if mark.get('mark_type') == 'mandatory_mark':
                    actual[key, str(exemplar.get('exemplar_id')), marking_codes.comparison_key(mark.get('mark'))] = mark.get('check_status')
                    if mark.get('error_codes') or status in {'error', 'invalid'}:
                        errors[error_key] = 'Код «Честный знак» не принят Ozon'
                elif mark.get('error_codes') or status in {'error', 'invalid'}:
                    errors[f'data:{key}:{unit}:imei'] = 'Ozon отклонил данные поля'
                failed = failed or status == 'failed'
    return actual, errors, failed


def poll(account: dict, order_id: str, context: dict, codes: list[dict]) -> dict:
    data = client.get_client(account).exemplars_status(order_id)
    if str(data.get('posting_number') or '') != order_id:
        return {'status': 'checking'}
    actual, errors, failed = _checks(data, context)
    if errors:
        return {'status': 'error', 'errors': errors}
    if failed:
        return {'status': 'retry', 'message': 'Ozon не смог обработать проверку кодов «Честный знак». Передача будет повторена.'}
    exemplar_ids = {str(p['product_id']): [str(e['exemplar_id']) for e in p['exemplars']] for p in context['products']}
    passed = all(str(actual.get((c['item_key'], exemplar_ids[c['item_key']][c['unit_no'] - 1], marking_codes.comparison_key(c['raw'])))).lower() in {'passed', 'success'}
                 for c in codes)
    details_ok, detail_errors = _checked_details(data, context)
    if detail_errors:
        return {'status': 'error', 'errors': detail_errors}
    passed = passed and details_ok
    has_data = bool(codes or context.get('_panel_details'))
    if passed and has_data and data.get('status') in {'ship_available', 'update_available'}:
        return {'status': 'accepted'}
    if passed and has_data:
        raw = client.get_client(account).posting_get(order_id, with_exemplars=True)
        if raw and raw.get('status') == store.STATUS_AWAITING_DELIVER and _applied(raw, codes, context):
            return {'status': 'accepted'}
    return {'status': 'checking'}


def _checked_details(data: dict, context: dict) -> tuple[bool, dict]:
    rows = [(str(p['product_id']), str(e.get('exemplar_id')), e) for p in data.get('products') or []
            for e in p.get('exemplars') or []]
    actual = {(key, exemplar_id): e for key, exemplar_id, e in rows}
    if context.get('_panel_details'):
        expected = {(str(p['product_id']), str(e['exemplar_id'])) for p in context['products'] for e in p['exemplars']}
        if len(actual) != len(rows) or set(actual) != expected:
            return False, {}  # Partial or duplicated readback cannot confirm GTD.
    good, errors = True, {}
    for product in context['products']:
        key = str(product['product_id'])
        for unit, e in enumerate(product['exemplars'], 1):
            got = actual.get((key, str(e['exemplar_id'])), {})
            for field, value in exemplar_data.at(context.get('_panel_details', {}), key, unit).items():
                absent = field == 'gtd' and value is None
                if field in {'gtd', 'rnpt', 'weight'}:
                    status = got.get(field + '_check_status')
                    matches = (got.get('is_gtd_absent') is True and not got.get('gtd')) if absent else (
                        got.get(field) == value and not got.get('is_' + field + '_absent'))
                    bad = got.get(field + '_error_codes')
                else:
                    mark = next((m for m in got.get('marks') or [] if m.get('mark_type') == 'imei' and m.get('mark') == value), {})
                    status, matches, bad = mark.get('check_status'), bool(mark), mark.get('error_codes')
                if bad or str(status).lower() in {'error', 'invalid'}:
                    errors[f'data:{key}:{unit}:{field}'] = 'Площадка отклонила значение поля'
                checked = str(status).lower() in {'passed', 'success'} or absent and status in (None, '')
                if not matches or not checked:
                    good = False
    return good, errors


def _applied(raw: dict, codes: list[dict], context: dict | None = None) -> bool:
    got = {(str(p.get('sku')), marking_codes.comparison_key(e.get('mandatory_mark'))) for p in (raw.get('product_exemplars') or {}).get('products') or []
           for e in p.get('exemplars') or []}
    if not all((c['item_key'], marking_codes.comparison_key(c['raw'])) in got for c in codes):
        return False
    actual = {(str(p.get('sku')), str(e.get('exemplar_id'))): e
              for p in (raw.get('product_exemplars') or {}).get('products') or [] for e in p.get('exemplars') or []}
    for product in (context or {}).get('products', []):
        key = str(product['product_id'])
        for unit, e in enumerate(product['exemplars'], 1):
            row = actual.get((key, str(e['exemplar_id'])), {})
            for field, value in exemplar_data.at(context.get('_panel_details', {}), key, unit).items():
                if field == 'imei':
                    if value not in (row.get('imei') or []):
                        return False
                elif field == 'gtd' and value is None:
                    if not row or row.get('is_gtd_absent') is not True or row.get('gtd'):
                        return False
                elif row.get(field) != value or row.get('is_' + field + '_absent'):
                    return False
    return True


def finalize(account: dict, order_id: str, context: dict, codes: list[dict]) -> None:
    api = client.get_client(account)
    raw = api.posting_get(order_id, with_exemplars=True)
    if not raw:
        raise ValueError('Ozon не подтвердил отправление')
    if raw.get('status') == store.STATUS_AWAITING_DELIVER:
        if not _applied(raw, codes, context):
            current = _remote_marks(api.exemplars_get(order_id))
            if any((c['item_key'], marking_codes.comparison_key(c['raw'])) not in current for c in codes):
                raise ValueError('Ozon не подтвердил актуальную маркировку. Повторная запись не выполнена.')
            readiness.attempt(account, order_id, lambda: api.exemplars_update(order_id))
            raw = api.posting_get(order_id, with_exemplars=True)
            if not raw or not _applied(raw, codes, context):
                raise ValueError('Ozon пока не подтвердил сохранение данных экземпляров в отправлении')
        readiness.confirmed(account, order_id)
    elif raw.get('status') != store.STATUS_AWAITING_PACKAGING:
        raise ValueError('Отправление Ozon вышло из рабочего статуса')


def redistribute(account, user, order_id, codes):
    from . import cargo
    return cargo.redistribute(account, user, order_id, codes)


def replacement_snapshot(account: dict, order_id: str, context: dict, codes: list[dict]) -> set[str] | None:
    api = client.get_client(account)
    data = api.exemplars_get(order_id)
    if str(data.get('posting_number') or '') != order_id:
        return None
    slots = {str(p['product_id']): [str(e['exemplar_id']) for e in p['exemplars']] for p in context['products']}
    expected = {key: set(ids) for key, ids in slots.items()}
    products = data.get('products') or []
    if len(products) != len(expected) or {str(p.get('product_id')) for p in products} != set(expected):
        return None
    identities, by_slot = set(), {}
    for product in products:
        key = str(product['product_id'])
        exemplars = product.get('exemplars') or []
        if (int(product.get('quantity') or 0) != len(expected[key]) or len(exemplars) != len(expected[key])
                or {str(e.get('exemplar_id')) for e in exemplars} != expected[key]):
            return None
        for exemplar in exemplars:
            values = set()
            for mark in exemplar.get('marks') or []:
                if mark.get('mark_type') in {'imei', 'jw_uin'}:
                    continue
                # Unexpected or short CIS cannot be treated as proof of absence.
                identity = marking._fingerprint(marking_codes.parse(mark.get('mark')))
                identities.add(identity)
                values.add(marking_codes.comparison_key(mark['mark']))
            by_slot[key, str(exemplar['exemplar_id'])] = values
    for code in codes:
        exemplar_id = slots[code['item_key']][code['unit_no'] - 1]
        if marking_codes.comparison_key(code['raw']) not in by_slot[code['item_key'], exemplar_id]:
            return None
    return identities if poll(account, order_id, context, codes)['status'] == 'accepted' else None


SOURCE = MarkingSource(prepare, submit, poll, finalize, lambda item: str(item.get('sku') or ''),
                       discover=lambda account, order_id: prepare(account, order_id, discover=True), redistribute=redistribute,
                       replacement_snapshot=replacement_snapshot)
