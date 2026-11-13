"""FBS Маркета: полная раскладка без удаления позиций; проверка каждого CIS."""
from __future__ import annotations

from copy import deepcopy

from ...core import db, marking
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
        raise ValueError('Автоматическая сборка и передача ЧЗ поддерживается для FBS и Экспресс Маркета')
    local = {str(i['item_id']): (str(i['offer_id']), int(i['quantity'])) for i in db.query(
        'SELECT item_id,offer_id,quantity FROM yandex_order_items WHERE account_id=? AND order_id=?',
        (account['id'], order_id))}
    products = []
    for item in raw.get('items') or []:
        key, sku, quantity = str(item.get('id') or ''), str(item.get('offerId') or ''), int(item.get('count') or 0)
        if local.get(key) != (sku, quantity):
            raise ValueError('Состав Маркета изменился. Обновите заказы перед сборкой.')
        types = set(item.get('requiredInstanceTypes') or [])
        products.append({'key': key, 'sku': sku, 'quantity': quantity,
                         'required': 'CIS' in types, 'possible': bool(types & {'CIS', 'CIS_OPTIONAL'})})
    if {p['key'] for p in products} != set(local):
        raise ValueError('Маркет вернул неполный состав заказа')
    return {'products': products, 'context': {'campaign': campaign, 'order': raw}}


def _same_items(left: dict, right: dict) -> bool:
    def shape(raw):
        return {(str(i.get('id')), str(i.get('offerId')), int(i.get('count') or 0)) for i in raw.get('items') or []}
    return shape(left) == shape(right)


def _instances(raw: dict) -> dict[str, list[dict]]:
    return {str(i['id']): i.get('instances') or [] for i in raw.get('items') or []}


def _saved_codes(raw: dict, codes: list[dict]) -> bool:
    instances = _instances(raw)
    return all(any(i.get('cisFull') == c['raw'] for i in instances.get(c['item_key'], [])) for c in codes)


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
            raise ValueError('Маркет не вернул раскладку нескольких коробок. Обновите заказ перед передачей ЧЗ.')
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
    if raw.get('substatus') == 'READY_TO_SHIP':
        if _saved_codes(raw, codes):
            return  # коды уже переданы, теперь проверяем их результат
        raise marking.Rejected({c['item_key']: 'Маркет запрещает менять раскладку после готовности к отгрузке' for c in codes})
    if raw.get('substatus') != 'STARTED':
        raise ValueError('Маркет пока не разрешает передать раскладку заказа')
    boxes = _boxes(raw, codes)
    dispatch()
    api.order_boxes(context['campaign'], order_id, boxes)


def poll(account: dict, order_id: str, context: dict, codes: list[dict]) -> dict:
    api = client.get_client(account)
    raw = api.order_details(order_id)
    if str(raw.get('campaignId')) != context['campaign'] or not _same_items(context['order'], raw):
        return {'status': 'error', 'errors': {c['item_key']: 'Состав заказа изменился' for c in codes}}
    # Доказываем, что проверяется именно последний полный код, не старый CIS.
    if not _saved_codes(raw, codes):
        return {'status': 'checking'}
    data = api.identifiers_status(context['campaign'], order_id)
    if data.get('status') != 'OK':
        return {'status': 'checking'}
    by_item = {str(i.get('id')): i.get('cis') or [] for i in (data.get('result') or {}).get('items') or []}
    errors = {}
    pending = False
    failed = False
    for code in codes:
        found = [c for c in by_item.get(code['item_key'], [])
                 if c.get('value') in {code['raw'], code['raw'].split('\x1d', 1)[0]}]
        if len(found) != 1:
            pending = True
            continue
        status = found[0].get('status')
        if status == 'INVALID':
            errors[f'{code["item_key"]}:{code["unit_no"]}'] = 'Маркет не разрешил продажу товара с этим ЧЗ'
        elif status != 'OK':
            pending = True  # FAILED — не удалось проверить; это не INVALID.
            failed = failed or status == 'FAILED'
    if errors:
        return {'status': 'error', 'errors': errors}
    if failed and raw.get('substatus') == 'STARTED':
        return {'status': 'retry', 'message': 'Маркет не смог проверить ЧЗ. Передача будет повторена.'}
    return {'status': 'checking' if pending or not codes else 'accepted'}


def finalize(account: dict, order_id: str, _context: dict, codes: list[dict]) -> None:
    if not _saved_codes(client.get_client(account).order_details(order_id), codes):
        raise ValueError('Маркет пока не подтвердил сохранение полного ЧЗ')


SOURCE = MarkingSource(prepare, submit, poll, finalize, lambda item: str(item.get('item_id') or ''))
