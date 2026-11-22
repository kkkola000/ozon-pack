"""FBS Маркета: полная раскладка без удаления позиций; проверка каждого CIS."""
from __future__ import annotations

from copy import deepcopy
from collections import Counter

from ...core import db, exemplar_data, marking, marking_codes
from ..base import MarkingSource
from . import client


def _campaign(account: dict, order_id: str) -> str:
    row = db.query_one('SELECT campaign_id FROM yandex_orders WHERE account_id=? AND id=?', (account['id'], order_id))
    if not row or not row['campaign_id']:
        raise ValueError('Не найден магазин заказа Яндекс Маркета')
    return str(row['campaign_id'])


def prepare(account: dict, order_id: str) -> dict:
    campaign = _campaign(account, order_id)
    raw = client.get_client(account).order_details(order_id)
    if str(raw.get('campaignId')) != campaign:
        raise ValueError('Маркет вернул заказ другого магазина')
    if raw.get('programType') not in {'FBS', 'EXPRESS'}:
        raise ValueError('Автоматическая сборка и передача кодов «Честный знак» поддерживается для FBS и Экспресс Маркета')
    local = {str(i['item_id']): (str(i['offer_id']), int(i['quantity'])) for i in db.query(
        'SELECT item_id,offer_id,quantity FROM yandex_order_items WHERE account_id=? AND order_id=?',
        (account['id'], order_id))}
    products = []
    details = {}
    for item in raw.get('items') or []:
        key, sku, quantity = str(item.get('id') or ''), str(item.get('offerId') or ''), int(item.get('count') or 0)
        if local.get(key) != (sku, quantity):
            raise ValueError('Состав Маркета изменился. Обновите заказы перед сборкой.')
        types = set(item.get('requiredInstanceTypes') or [])
        products.append({'key': key, 'sku': sku, 'quantity': quantity,
                         'required': bool(types & {'CIS', 'CIS_OPTIONAL'}),
                         'possible': bool(types & {'CIS', 'CIS_OPTIONAL'}),
                         'fields': [f.lower() for f in ('GTD', 'RNPT', 'UIN') if f in types]})
        for unit, instance in enumerate(item.get('instances') or [], 1):
            values = {f: instance[f] for f in products[-1]['fields'] if instance.get(f)}
            if values:
                details.setdefault(key, {})[str(unit)] = values
    if {p['key'] for p in products} != set(local):
        raise ValueError('Маркет вернул неполный состав заказа')
    return {'products': products, 'context': {'campaign': campaign, 'order': raw}, 'details': details}


def _same_items(left: dict, right: dict) -> bool:
    def shape(raw):
        return {(str(i.get('id')), str(i.get('offerId')), int(i.get('count') or 0)) for i in raw.get('items') or []}
    return shape(left) == shape(right)


def _instances(raw: dict) -> dict[str, list[dict]]:
    return {str(i['id']): i.get('instances') or [] for i in raw.get('items') or []}


def _missing_codes(raw: dict, codes: list[dict]) -> list[dict]:
    instances = _instances(raw)
    got = Counter((key, marking_codes.comparison_key(i.get('cisFull'))) for key, rows in instances.items() for i in rows)
    missing = []
    for code in codes:
        key = code['item_key'], marking_codes.comparison_key(code['raw'])
        if got[key]:
            got[key] -= 1
        else:
            missing.append(code)
    return missing


def _saved_codes(raw: dict, codes: list[dict]) -> bool:
    return not _missing_codes(raw, codes)


def _compatible_codes(raw: dict, codes: list[dict]) -> bool:
    wanted = Counter((c['item_key'], marking_codes.comparison_key(c['raw'])) for c in codes)
    keys = {c['item_key'] for c in codes}
    instances = [(key, instance) for key, rows in _instances(raw).items() if key in keys for instance in rows]
    if any(instance.get('cis') and not instance.get('cisFull') for _, instance in instances):
        return False  # A short CIS alone cannot prove which full code the marketplace holds.
    actual = Counter((key, marking_codes.comparison_key(instance['cisFull']))
                     for key, instance in instances if instance.get('cisFull'))
    return not (actual - wanted)


def _closed_layout(raw: dict, context: dict, codes: list[dict]) -> None:
    if raw.get('status') == 'PROCESSING' and raw.get('substatus') == 'READY_TO_SHIP':
        missing = _missing_codes(raw, codes)
        if missing:
            missing_units = {(c['item_key'], c['unit_no']) for c in missing}
            present = [c for c in codes if (c['item_key'], c['unit_no']) not in missing_units]
            raise marking.NotRecorded({f'{c["item_key"]}:{c["unit_no"]}': marking.NOT_RECORDED for c in missing},
                                      local_completion=_saved_data(raw, context, present) and _compatible_codes(raw, codes))


def with_details(raw: dict, details: dict, *, layout: bool = True) -> dict:
    raw = deepcopy(raw)
    for item in raw['items']:
        key = str(item['id'])
        if key not in details:
            continue
        instances = item.setdefault('instances', [])
        while len(instances) < int(item['count']):
            instances.append({})
        for unit in range(1, int(item['count']) + 1):
            instances[unit - 1].update(exemplar_data.at(details, key, unit))
    if not layout:
        return raw
    # boxesLayout.instances can contain older values: overlay by the same
    # physical-unit offsets used for CIS when constructing the full request.
    positions = {}
    for box in (raw.get('delivery') or {}).get('boxesLayout') or []:
        for entry in box.get('items') or []:
            key = str(entry['id'])
            if key not in details:
                continue
            item = next(i for i in raw['items'] if str(i['id']) == key)
            start, count = _box_range(entry, item, positions, key)
            prior = entry.get('instances') or item.get('instances') or []
            if not entry.get('instances'):
                prior = prior[start:start + count]
            entry['instances'] = [{**(prior[n] if n < len(prior) else {}),
                                   **exemplar_data.at(details, key, start + n + 1)} for n in range(count)]
    return raw


def _saved_data(raw: dict, context: dict, codes: list[dict]) -> bool:
    if not _saved_codes(raw, codes):
        return False
    details = context.get('_panel_details', {})
    for key, rows in details.items():
        actual = list(_instances(raw).get(key, []))
        for unit, fields in rows.items():
            mark = next((c['raw'] for c in codes if c['item_key'] == key and c['unit_no'] == int(unit)), None)
            match = next((i for i in actual if all(i.get(f) == v for f, v in fields.items()) and
                          (not mark or marking_codes.equivalent(i.get('cisFull'), mark))), None)
            if match is None:
                return False
            actual.remove(match)
    return True


def _box_instances(entry, item, start, quantity, codes):
    previous = entry.get('instances') or (item.get('instances') or [])[start:start + quantity]
    instances = []
    for offset in range(quantity):
        # Сохраняем УИН/ГТД/РНПТ/страну; cisFull — только поле ответа.
        base = previous[offset] if offset < len(previous) else {}
        instance = {k: v for k, v in base.items() if k in {'uin', 'rnpt', 'gtd', 'countryCode', 'cis'}}
        code = next((c for c in codes if c['item_key'] == str(item['id']) and c['unit_no'] == start + offset + 1), None)
        if code:
            instance['cis'] = code['raw']
        elif base.get('cisFull'):
            instance['cis'] = base['cisFull']
        if instance:
            instances.append(instance)
    return instances


def _box_range(entry, item, positions, key):
    if entry.get('partialCount'):
        # Нельзя угадывать, к какой одинаковой единице относится часть.
        if int(item['count']) != 1:
            raise ValueError('Для нескольких одинаковых многоместных товаров нужна сверка раскладки')
        start, quantity = 0, 1
    else:
        quantity = int(entry.get('fullCount') or 0)
        if quantity < 1:
            raise ValueError('В раскладке Маркета не указано количество товара')
        start = positions.get(key, 0)
        positions[key] = start + quantity
    return start, quantity


def _boxes(raw: dict, codes: list[dict]) -> list[dict]:
    items = {str(i['id']): i for i in raw['items']}
    layout = deepcopy((raw.get('delivery') or {}).get('boxesLayout') or [])
    if not layout:
        if len((raw.get('delivery') or {}).get('boxes') or []) > 1:
            raise ValueError('Маркет не вернул раскладку нескольких коробок. Обновите заказ перед передачей кодов «Честный знак».')
        layout = [{'items': [{'id': int(k), 'fullCount': int(i['count'])} for k, i in items.items()]}]
    positions = {}
    counts = {k: 0 for k in items}
    out = []
    for box in layout:
        clean_items = []
        for entry in box.get('items') or []:
            key = str(entry.get('id'))
            if key not in items:
                raise ValueError('В раскладке Маркета найдена неизвестная позиция')
            start, quantity = _box_range(entry, items[key], positions, key)
            clean = {k: deepcopy(entry[k]) for k in ('id', 'fullCount', 'partialCount') if k in entry}
            instances = _box_instances(entry, items[key], start, quantity, codes)
            if instances:
                clean['instances'] = instances
            counts[key] = max(counts[key], 1) if entry.get('partialCount') else counts[key] + quantity
            clean_items.append(clean)
        out.append({'items': clean_items})
    if counts != {k: int(i['count']) for k, i in items.items()}:
        raise ValueError('Раскладка Маркета не соответствует полному составу заказа')
    return out


def submit(account: dict, order_id: str, context: dict, codes: list[dict], dispatch) -> None:
    api = client.get_client(account)
    raw = api.order_details(order_id)
    if str(raw.get('campaignId')) != context['campaign'] or not _same_items(context['order'], raw):
        raise marking.Rejected({c['item_key']: 'Состав заказа Маркета изменился' for c in codes})
    if raw.get('status') != 'PROCESSING':
        raise ValueError('Заказ Маркета вышел из рабочего статуса')
    from ...core import cargo
    plan = cargo.get(account['id'], order_id)
    if plan and raw.get('substatus') == 'STARTED':
        user = db.query_one('SELECT id,login,role FROM users WHERE id=(SELECT user_id FROM marking_orders '
                            'WHERE account_id=? AND order_id=?)', (account['id'], order_id))
        if not user:
            raise ValueError('Не найден сборщик заказа')
        cargo.apply(account, order_id, dict(user), plan['boxes'], codes, dispatch)
        return
    if raw.get('substatus') == 'READY_TO_SHIP':
        if _saved_data(raw, context, codes):
            return  # коды уже переданы, теперь проверяем их результат
        _closed_layout(raw, context, codes)
        raise marking.Rejected({'data:' + key + ':' + unit: 'Маркет запрещает менять данные после готовности к отгрузке. Исправьте заказ в кабинете Маркета.'
                                for key, rows in context.get('_panel_details', {}).items() for unit in rows} or
                               {c['item_key']: 'Маркет запрещает менять раскладку после готовности к отгрузке' for c in codes})
    if raw.get('substatus') != 'STARTED':
        raise ValueError('Маркет пока не разрешает передать раскладку заказа')
    boxes = _boxes(with_details(raw, context.get('_panel_details', {})), codes)
    dispatch()
    api.order_boxes(context['campaign'], order_id, boxes)


def poll(account: dict, order_id: str, context: dict, codes: list[dict]) -> dict:
    api = client.get_client(account)
    raw = api.order_details(order_id)
    if str(raw.get('campaignId')) != context['campaign'] or not _same_items(context['order'], raw):
        return {'status': 'error', 'errors': {c['item_key']: 'Состав заказа изменился' for c in codes}}
    # Доказываем, что проверяется именно последний полный код, не старый CIS.
    if not _saved_data(raw, context, codes):
        _closed_layout(raw, context, codes)
        return {'status': 'checking'}
    if not codes:
        return {'status': 'accepted' if context.get('_panel_details') else 'checking'}
    data = api.identifiers_status(context['campaign'], order_id)
    if data.get('status') != 'OK':
        return {'status': 'checking'}
    return _code_checks(data, raw, codes)


def _code_checks(data, raw, codes):
    by_item = {str(i.get('id')): i.get('cis') or [] for i in (data.get('result') or {}).get('items') or []}
    errors = {}
    pending = False
    failed = False
    for code in codes:
        found = [c for c in by_item.get(code['item_key'], [])
                 if c.get('value') == marking_codes.short(code['raw']) or marking_codes.equivalent(c.get('value'), code['raw'])]
        if len(found) != 1:
            pending = True
            continue
        status = found[0].get('status')
        if status == 'INVALID':
            errors[f'{code["item_key"]}:{code["unit_no"]}'] = 'Маркет не разрешил продажу товара с этим кодом «Честный знак»'
        elif status != 'OK':
            pending = True  # FAILED — не удалось проверить; это не INVALID.
            failed = failed or status == 'FAILED'
    if errors:
        return {'status': 'error', 'errors': errors}
    if failed and raw.get('substatus') == 'STARTED':
        return {'status': 'retry', 'message': 'Маркет не смог проверить Честный знак. Передача будет повторена.'}
    return {'status': 'checking' if pending or not codes else 'accepted'}


def finalize(account: dict, order_id: str, context: dict, codes: list[dict]) -> None:
    if not _saved_data(client.get_client(account).order_details(order_id), context, codes):
        raise ValueError('Маркет пока не подтвердил сохранение полных данных экземпляров')


def complete_local(account: dict, order_id: str, context: dict, codes: list[dict]) -> None:
    """Read-only exception: finish an already-ready order, keeping missing CIS unaccepted."""
    api = client.get_client(account)
    raw = api.order_details(order_id)
    if (str(raw.get('campaignId')) != context['campaign'] or not _same_items(context['order'], raw)
            or raw.get('programType') not in {'FBS', 'EXPRESS'}
            or raw.get('status') != 'PROCESSING' or raw.get('substatus') != 'READY_TO_SHIP'):
        raise ValueError('Маркет изменил состав или состояние заказа. Обновите заказ перед завершением.')
    missing = {(c['item_key'], c['unit_no']) for c in _missing_codes(raw, codes)}
    present = [c for c in codes if (c['item_key'], c['unit_no']) not in missing]
    if not _saved_data(raw, context, present):
        raise ValueError('Маркет не подтвердил остальные обязательные данные экземпляров. Завершение заблокировано.')
    if not _compatible_codes(raw, codes):
        raise ValueError('В заказе Маркета записан другой Честный знак. Сверьте коды перед завершением.')
    if present:
        checked = api.identifiers_status(context['campaign'], order_id)
        if checked.get('status') != 'OK' or _code_checks(checked, raw, present)['status'] != 'accepted':
            raise ValueError('Маркет не подтвердил проверку уже записанных кодов. Завершение заблокировано.')


SOURCE = MarkingSource(prepare, submit, poll, finalize, lambda item: str(item.get('item_id') or ''),
                       local_complete=complete_local)
