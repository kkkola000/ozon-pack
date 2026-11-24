"""FBS Маркета: полная раскладка без удаления позиций; проверка каждого CIS."""
from __future__ import annotations

from copy import deepcopy
from collections import Counter

from ...core import db, exemplar_data, marking, marking_codes
from ..base import MarkingSource
from . import cis_status, client


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
                         'fields': [f.lower() for f in ('GTD', 'RNPT') if f in types]})
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


def _cis_text(value):
    if not isinstance(value, str):
        return None
    value = value.rstrip('\r\n')
    if value.startswith((']d2', ']d8')):
        value = value[3:]
    return value.removeprefix(marking_codes.GS).removesuffix(marking_codes.GS)


def _full_cis(value):
    if not isinstance(value, str):
        return None
    try:
        return marking_codes.parse(value)
    except ValueError:
        try:
            return marking_codes.parse(_cis_text(value))
        except ValueError:
            return None


def _short_forms(parsed):
    return {'01' + parsed.gtin + '21' + parsed.serial, parsed.gtin + parsed.serial}


def _cis_value_matches(value, full_code):
    expected = marking_codes.parse(full_code)
    actual = _full_cis(value)
    if actual:
        return actual.fields == expected.fields  # Never ignore a different crypto tail.
    return _cis_text(value) in _short_forms(expected)


def _read_instance(instance):
    value = instance.get('cisFull') or instance.get('cis')
    parsed = _full_cis(value)
    return {**instance, 'cisFull': parsed.raw} if parsed else instance


def _short_readback_agrees(direct, boxed):
    forms, remaining = {}, Counter()
    for instance in boxed:
        parsed = marking_codes.parse(instance['cisFull'])
        remaining[parsed.fields] += 1
        for form in _short_forms(parsed):
            forms.setdefault(form, set()).add(parsed.fields)
    for instance in direct:
        if not instance.get('cis'):
            continue
        candidates = forms.get(_cis_text(instance['cis']), set())
        if len(candidates) != 1:
            return False
        match = next(iter(candidates))
        if not remaining[match]:
            return False
        remaining[match] -= 1
    return True


def _instances(raw: dict) -> dict[str, list[dict]]:
    layout = _layout_instances(raw)
    result = {}
    for item in raw.get('items') or []:
        key = str(item['id'])
        direct = [_read_instance(i) for i in item.get('instances') or []]
        boxed = layout.get(key, [])
        # Never replace a conflicting direct full-code response with a nicer
        # box response. A short direct CIS must also agree with the full layout.
        if boxed and not any(i.get('cisFull') for i in direct) and _short_readback_agrees(direct, boxed):
            direct = boxed
        result[key] = direct
    return result


def _layout_instances(raw: dict) -> dict[str, list[dict]]:
    """Full CIS from the documented box layout, with complete physical-unit coverage."""
    if not (raw.get('delivery') or {}).get('boxesLayout'):
        return {}
    try:
        return _complete_layout_instances(raw)
    except (ValueError, KeyError, TypeError, IndexError):
        return {}


def _complete_layout_instances(raw):
    layout = raw['delivery']['boxesLayout']
    if any(e.get('instances') and len(e['instances']) != int(e.get('fullCount') or 1)
           for box in layout for e in box.get('items') or []):
        return {}
    items = {str(i['id']): i for i in raw['items']}
    slots = {key: [None] * int(item['count']) for key, item in items.items()}
    positions = {}
    for box in _boxes(raw, []):  # Validates all products, counts and partial boxes.
        for entry in box['items']:
            key = str(entry['id'])
            start, count = _box_range(entry, items[key], positions, key)
            instances = entry.get('instances') or []
            if not instances:
                continue
            if len(instances) != count:
                return {}
            for offset, instance in enumerate(instances):
                value = instance.get('cis')
                if not value:
                    continue
                parsed = _full_cis(value)  # Short CIS never proves crypto was stored.
                if not parsed:
                    return {}
                full = {**instance, 'cisFull': parsed.raw}
                unit = start + offset
                previous = slots[key][unit]
                if previous and (not marking_codes.equivalent(previous['cisFull'], full['cisFull']) or
                                 any(previous.get(f) != full.get(f) for f in ('gtd', 'rnpt', 'countryCode'))):
                    return {}
                slots[key][unit] = full
    return {key: values for key, values in slots.items() if all(values)}


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
    instances = _instances(raw)
    return not _missing_codes(raw, codes) and all(len(instances[str(i['id'])]) <= int(i['count']) for i in raw['items'])


def _compatible_codes(raw: dict, codes: list[dict]) -> bool:
    wanted = Counter((c['item_key'], marking_codes.comparison_key(c['raw'])) for c in codes)
    keys = {c['item_key'] for c in codes}
    by_item = _instances(raw)
    if any(len(by_item[str(i['id'])]) > int(i['count']) for i in raw['items']):
        return False
    instances = [(key, instance) for key, rows in by_item.items() if key in keys for instance in rows]
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
            instances[unit - 1] = exemplar_data.overlay(instances[unit - 1], exemplar_data.at(details, key, unit))
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
            entry['instances'] = [exemplar_data.overlay(prior[n] if n < len(prior) else {},
                                  exemplar_data.at(details, key, start + n + 1)) for n in range(count)]
    return raw


def _saved_data(raw: dict, context: dict, codes: list[dict]) -> bool:
    if not _saved_codes(raw, codes):
        return False
    details = context.get('_panel_details', {})
    for key, rows in details.items():
        actual = list(_instances(raw).get(key, []))
        # Match populated instances first: Yandex may drop entirely empty ones.
        for unit, fields in sorted(rows.items(), key=lambda row: row[1] == {'gtd': None}):
            mark = next((c['raw'] for c in codes if c['item_key'] == key and c['unit_no'] == int(unit)), None)
            match = next((i for i in actual if exemplar_data.matches(i, fields) and
                          (not mark or marking_codes.equivalent(i.get('cisFull'), mark))), None)
            if fields == {'gtd': None} and not mark and not actual:
                continue  # Yandex can omit the entire empty instance after GTD was omitted.
            if match is None:
                return False
            actual.remove(match)
    return True


def _box_instances(entry, item, start, quantity, codes):
    previous = entry.get('instances') or (item.get('instances') or [])[start:start + quantity]
    instances = []
    for offset in range(quantity):
        # Preserve existing remote fields; cisFull is response-only.
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
    if raw.get('status') != 'PROCESSING' or raw.get('programType') not in {'FBS', 'EXPRESS'}:
        raise ValueError('Маркет изменил состояние или схему работы заказа. Обновите заказ перед продолжением.')
    # A rejected/pending CIS may not yet appear in instances. Read its check
    # independently; absence of cisFull must never hide an INVALID response.
    data = api.identifiers_status(context['campaign'], order_id) if codes else {}
    outcome = _code_checks(data, raw, codes) if codes else None
    if outcome and outcome['status'] == 'error':
        return outcome
    if codes and _retry_rescan(raw, context, codes, data, outcome):
        return {**outcome, 'status': 'retry'}
    # Acceptance still requires both the stored full code and its own OK.
    if not _saved_data(raw, context, codes):
        _closed_layout(raw, context, codes)
        if outcome and outcome['status'] == 'retry':
            return outcome
        return {'status': 'checking', 'checks': outcome.get('checks') if outcome else None}
    if not codes:
        return {'status': 'accepted' if context.get('_panel_details') else 'checking'}
    return outcome


def _retry_rescan(raw, context, codes, data, outcome):
    # An explicit scan can resume a definitively acknowledged write that left
    # no CIS behind. An uncertain dispatch never enables this context flag.
    if (not context.get('_panel_rescan') or raw.get('substatus') != 'STARTED'
            or data.get('status') != 'OK' or not _compatible_codes(raw, codes)):
        return False
    if not isinstance(data.get('result'), dict) or not isinstance(data['result'].get('items'), list):
        return False
    checks = [c for rows in _cis_by_item(data).values() for c in rows]
    if not checks:
        return bool(_missing_codes(raw, codes))
    statuses = [c.get('status') for c in outcome['checks'].values()]
    return all(s in {'OK', 'NOT_ON_VALIDATION'} for s in statuses) and 'NOT_ON_VALIDATION' in statuses


def _cis_by_item(data):
    # Other identifier types never prove CIS acceptance.
    by_item = {}
    for item in (data.get('result') or {}).get('items') or []:
        # Keep repeated entries: an ambiguous response must not overwrite a failed check with OK.
        by_item.setdefault(str(item.get('id')), []).extend(item.get('cis') or [])
    return by_item


def _code_checks(data, raw, codes):
    by_item = _cis_by_item(data) if data.get('status') == 'OK' else {}
    errors = {}
    checks = {}
    pending = False
    failed = False
    for code in codes:
        key = f'{code["item_key"]}:{code["unit_no"]}'
        found = [c for c in by_item.get(code['item_key'], []) if _cis_value_matches(c.get('value'), code['raw'])]
        checks[key] = cis_status.describe(found[0] if len(found) == 1 else {})
        if len(found) != 1:
            pending = True
            continue
        status = found[0].get('status')
        if status == 'INVALID':
            errors[key] = checks[key]['message']
        elif status != 'OK':
            pending = True  # FAILED — не удалось проверить; это не INVALID.
            failed = failed or status == 'FAILED'
    if errors:
        return {'status': 'error', 'errors': errors, 'checks': checks}
    if failed and raw.get('substatus') == 'STARTED':
        return {'status': 'retry', 'message': cis_status.STATUSES['FAILED'], 'checks': checks}
    return {'status': 'checking' if pending or not codes else 'accepted', 'checks': checks}


def finalize(account: dict, order_id: str, context: dict, codes: list[dict]) -> dict:
    # Repeat both proofs before and after readiness: a previous OK can become stale.
    outcome = poll(account, order_id, context, codes)
    if outcome['status'] == 'error':
        raise marking.Rejected(outcome['errors'], 'Маркет не подтвердил проверку кодов «Честный знак»', checks=outcome.get('checks'))
    if outcome['status'] != 'accepted':
        raise marking.AwaitingCheck(outcome)
    return outcome


def replacement_snapshot(account: dict, order_id: str, context: dict, codes: list[dict]) -> set[str] | None:
    """A full order read plus fresh checks; a short CIS never proves absence."""
    api = client.get_client(account)
    raw = api.order_details(order_id)
    if (str(raw.get('campaignId')) != context['campaign'] or not _same_items(context['order'], raw)
            or raw.get('status') != 'PROCESSING' or raw.get('programType') not in {'FBS', 'EXPRESS'}
            or not _saved_data(raw, context, codes)):
        return None
    actual = _instances(raw)
    for item in raw['items']:
        instances = actual[str(item['id'])]
        if len(instances) != int(item['count']):
            return None
        for instance in instances:
            if instance.get('cis') and not instance.get('cisFull'):
                return None
    identities = _remote_cis_fingerprints(raw)
    if identities is None:
        return None
    if _code_checks(api.identifiers_status(context['campaign'], order_id), raw, codes)['status'] != 'accepted':
        return None
    return identities


def _remote_cis_fingerprints(raw):
    # For absence proofs inspect both sources, including conflicting box CIS.
    # Never discard an old mark merely because the direct item has a newer one.
    instances = [i for item in raw['items'] for i in item.get('instances') or []]
    instances += [i for box in (raw.get('delivery') or {}).get('boxesLayout') or []
                  for entry in box.get('items') or [] for i in entry.get('instances') or []]
    full, short = {}, []
    for instance in instances:
        for value in (instance.get('cisFull'), instance.get('cis')):
            if not value:
                continue
            parsed = _full_cis(value)
            if parsed:
                for form in _short_forms(parsed):
                    full.setdefault(form, set()).add(marking._fingerprint(parsed))
            else:
                short.append(_cis_text(value))
    if any(len(full.get(value, set())) != 1 for value in short):
        return None
    return {fingerprint for matches in full.values() for fingerprint in matches}


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
        if _code_checks(checked, raw, present)['status'] != 'accepted':
            raise ValueError('Маркет не подтвердил проверку уже записанных кодов. Завершение заблокировано.')


SOURCE = MarkingSource(prepare, submit, poll, finalize, lambda item: str(item.get('item_id') or ''),
                       local_complete=complete_local, replacement_snapshot=replacement_snapshot)
