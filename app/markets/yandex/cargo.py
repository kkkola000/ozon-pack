"""Раскладка Маркета: полный состав, экземпляры и фактические идентификаторы мест."""
from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from copy import deepcopy

from ...core import cargo, db, exemplar_data, marking
from ...core.config import settings
from ..base import CargoSource
from . import client, store

log = logging.getLogger(__name__)


def fresh(account: dict, order_id: str) -> dict:
    local = db.query_one('SELECT campaign_id FROM yandex_orders WHERE account_id=? AND id=?', (account['id'], order_id))
    if not local:
        raise ValueError('Заказ Маркета не найден')
    raw = client.get_client(account).order_details(order_id)
    if str(raw.get('campaignId')) != str(local['campaign_id']):
        raise ValueError('Маркет вернул заказ другого магазина')
    if raw.get('status') != 'PROCESSING' or raw.get('substatus') not in {'STARTED', 'READY_TO_SHIP'}:
        raise ValueError('Маркет больше не разрешает сборку этого заказа')
    expected = {(str(r['item_id']), str(r['offer_id']), int(r['quantity'])) for r in db.query(
        'SELECT item_id,offer_id,quantity FROM yandex_order_items WHERE account_id=? AND order_id=?', (account['id'], order_id))}
    actual = {(str(i['id']), str(i['offerId']), int(i['count'])) for i in raw.get('items') or []}
    if expected != actual:
        raise ValueError('Состав заказа изменился. Обновите заказы перед распределением.')
    return raw


def products(raw: dict) -> list[dict]:
    return [{'key': str(i['id']), 'sku': str(i['offerId']), 'quantity': int(i['count'])} for i in raw['items']]


def _cis(instance: dict) -> str:
    return instance.get('cisFull') or instance.get('cis') or ''


def _known_unit(product: dict, value: str) -> int | None:
    codes = [_cis(i) for i in product.get('instances') or []]
    if value and codes.count(value) == 1:
        return codes.index(value) + 1
    if value:
        raise ValueError('Маркет не подтвердил экземпляр товара в грузоместе')
    return None


def _partial_units(product: dict, entries: list[dict], used: set[int]) -> dict[int, list[str]]:
    groups = defaultdict(list)
    for entry in entries:
        if entry.get('partialCount'):
            values = [_cis(i) for i in entry.get('instances') or [] if _cis(i)]
            if len(values) > 1:
                raise ValueError('Часть товара содержит несколько ЧЗ')
            groups[values[0] if values else ''].append(entry)
    result = {}
    for value, group in groups.items():
        total = int(group[0]['partialCount'].get('total') or 0)
        numbers = [int(e['partialCount'].get('current') or 0) for e in group]
        if total < 2 or sorted(numbers) != list(range(1, total + 1)) or any(
                int(e['partialCount'].get('total') or 0) != total for e in group):
            raise ValueError('Маркет не вернул однозначный полный набор частей товара')
        unit = _known_unit(product, value) or next((n for n in range(1, int(product['count']) + 1) if n not in used), 0)
        if not unit or unit in used:
            raise ValueError('Маркет повторно использовал экземпляр товара')
        used.add(unit)
        for entry in group:
            result[id(entry)] = [cargo.token(str(product['id']), unit)]
    return result


def _whole_units(product: dict, entry: dict, used: set[int]) -> list[str]:
    count = int(entry.get('fullCount') or 0)
    instances = entry.get('instances') or []
    if count < 1 or instances and len(instances) != count:
        raise ValueError('Маркет не вернул полный состав грузоместа')
    result = []
    for index in range(count):
        value = _cis(instances[index]) if instances else ''
        unit = _known_unit(product, value) or next((n for n in range(1, int(product['count']) + 1) if n not in used), 0)
        if not unit or unit in used:
            raise ValueError('Маркет повторно использовал экземпляр товара')
        used.add(unit)
        result.append(cargo.token(str(product['id']), unit))
    return result


def decode(raw: dict, codes=()) -> list[dict]:
    layout = (raw.get('delivery') or {}).get('boxesLayout') or []
    known = {str(i['id']) for i in raw['items']}
    if any(str(e['id']) not in known or bool(e.get('partialCount')) == bool(e.get('fullCount'))
           for box in layout for e in box.get('items') or []):
        raise ValueError('Маркет вернул неизвестный товар или некорректное количество в грузоместе')
    members = {}
    for product in raw['items']:
        stable = {m['unit_no']: m['raw'] for m in codes if m['item_key'] == str(product['id'])}
        if stable and len(stable) == int(product['count']):
            product = {**product, 'instances': [{'cis': stable[n]} for n in range(1, int(product['count']) + 1)]}
        entries = [e for box in layout for e in box.get('items') or [] if str(e['id']) == str(product['id'])]
        used = set()
        members.update(_partial_units(product, entries, used))
        for entry in entries:
            if not entry.get('partialCount'):
                members[id(entry)] = _whole_units(product, entry, used)
    result = []
    for index, box in enumerate(layout, 1):
        result.append({'id': f'box-{index}', 'units': [u for e in box.get('items') or [] for u in members.get(id(e), [])],
                       'remote_id': str(box.get('boxId') or ''), 'barcode': str(box.get('barcode') or '')})
    if result:
        cargo.validate({'units': cargo.units(products(raw)), 'boxes': []}, result)
    return result


def remote_stamp(raw: dict) -> str:
    layout = (raw.get('delivery') or {}).get('boxesLayout') or []
    snapshot = sorted((str(b.get('boxId')), str(b.get('barcode')), _box_shape(b, codes=True)) for b in layout)
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()


def _snapshot(account: dict, order_id: str, raw: dict) -> dict:
    saved = cargo.get(account['id'], order_id)
    marked = marking.order(account['id'], order_id)
    stamp = remote_stamp(raw)
    boxes = (saved['boxes'] if saved and saved['status'] == 'confirmed' and saved['remote_stamp'] == stamp
             else decode(raw, marking.codes(marked, full=True) if marked else []))
    return {'products': products(raw), 'boxes': boxes, 'remote_stamp': stamp,
            'baseline': raw['substatus'] == 'READY_TO_SHIP' and not boxes}


def read(account: dict, order_id: str) -> dict:
    return _snapshot(account, order_id, fresh(account, order_id))


def _instance(raw: dict, unit: dict, marks: list[dict]) -> dict:
    item = next(i for i in raw['items'] if str(i['id']) == unit['key'])
    previous = item.get('instances') or []
    prior = previous[unit['unit'] - 1] if unit['unit'] <= len(previous) else {}
    result = {k: deepcopy(prior[k]) for k in ('uin', 'rnpt', 'gtd', 'countryCode') if k in prior}
    mark = next((m for m in marks if m['item_key'] == unit['key'] and m['unit_no'] == unit['unit']), None)
    value = mark['raw'] if mark else _cis(prior)
    if value:
        result['cis'] = value
    if 'CIS' in (item.get('requiredInstanceTypes') or []) and not value:
        raise ValueError('Сначала отсканируйте ЧЗ каждой обязательной товарной единицы')
    return result


def payload(raw: dict, units: list[dict], boxes: list[dict], marks: list[dict]) -> list[dict]:
    by_id = {u['id']: u for u in units}
    output = []
    for box in boxes:
        grouped = defaultdict(list)
        for token in box['units']:
            grouped[by_id[token]['key']].append(by_id[token])
        items = []
        for key, members in grouped.items():
            item = {'id': int(key)}
            places = [b for b in boxes if members[0]['id'] in b['units']]
            if len(places) > 1:
                item['partialCount'] = {'current': places.index(box) + 1, 'total': len(places)}
            else:
                item['fullCount'] = len(members)
            instances = [_instance(raw, u, marks) for u in sorted(members, key=lambda u: u['unit'])]
            if any(instances):
                if not all(instances):
                    raise ValueError('Маркет не подтвердил полный состав экземпляров коробки')
                item['instances'] = instances
            items.append(item)
        output.append({'items': items})
    return output


def _box_shape(box: dict, *, codes: bool) -> str:
    items = []
    for entry in box.get('items') or []:
        item = {k: entry[k] for k in ('id', 'fullCount', 'partialCount') if k in entry}
        if codes:
            item['instances'] = sorted(json.dumps({**{k: i[k] for k in ('gtd', 'rnpt', 'uin', 'countryCode') if k in i},
                                                  **({'cis': _cis(i)} if _cis(i) else {})}, sort_keys=True)
                                       for i in entry.get('instances') or [])
        items.append(item)
    return json.dumps(sorted(items, key=lambda i: int(i['id'])), sort_keys=True)


def confirm(raw: dict, expected: list[dict], boxes: list[dict]) -> list[dict]:
    actual = list((raw.get('delivery') or {}).get('boxesLayout') or [])
    if len(actual) != len(expected):
        raise ValueError('Маркет пока не подтвердил количество грузомест')
    with_codes = any(p.get('instances') for b in expected for p in b['items'])
    confirmed = []
    for proposal, local in zip(expected, boxes, strict=True):
        match = next((b for b in actual if _box_shape(b, codes=with_codes) == _box_shape(proposal, codes=with_codes)), None)
        if not match or not match.get('boxId') or not match.get('barcode'):
            raise ValueError('Маркет пока не подтвердил состав, ЧЗ и ярлыки грузомест')
        actual.remove(match)
        confirmed.append({'id': local['id'], 'units': local['units'],
                          'remote_id': str(match['boxId']), 'barcode': str(match['barcode'])})
    if len({b['remote_id'] for b in confirmed}) != len(confirmed) or len({b['barcode'] for b in confirmed}) != len(confirmed):
        raise ValueError('Маркет вернул повторяющиеся идентификаторы грузомест')
    return confirmed


def write(account, _user, order_id, units, boxes, marks, dispatch, uncertain):
    raw = fresh(account, order_id)
    if cargo._shape(cargo.units(products(raw))) != cargo._shape(units):
        raise ValueError('Состав заказа изменился')
    from .marking import with_details
    wanted = payload(with_details(raw, exemplar_data.values(marking.order(account['id'], order_id)), layout=False), units, boxes, marks)
    if not uncertain:
        saved = cargo.get(account['id'], order_id)
        if saved['remote_stamp'] and saved['remote_stamp'] != remote_stamp(raw):
            raise ValueError('Раскладка изменена на Маркете. Обновите заказ перед сохранением.')
        dispatch()
        client.get_client(account).order_boxes(str(raw['campaignId']), order_id, wanted)
        raw = fresh(account, order_id)
    confirmed = confirm(raw, wanted, boxes)
    with db.write() as conn:
        store.upsert_yandex_order(conn, account['id'], raw)
    return {'boxes': confirmed, 'remote_stamp': remote_stamp(raw)}


def prepare_ready(account: dict, user: dict, order_id: str):
    existing = cargo.get(account['id'], order_id)
    if not settings.split_orders_on_pack and not existing:
        return
    raw = fresh(account, order_id)
    snapshot = _snapshot(account, order_id, raw)
    if existing:
        cargo.reconcile(account, order_id, snapshot)
        return
    if raw['substatus'] == 'READY_TO_SHIP' and not snapshot['boxes']:
        return
    separate = raw['substatus'] == 'STARTED' and len(snapshot['boxes']) < 2
    if separate:
        snapshot['boxes'] = []
    saved = cargo.create(account, order_id, snapshot, user, separate=separate)
    if len(saved['units']) == 1 and not snapshot['boxes'] and saved['status'] == 'draft':
        # Один товар без ручной раскладки продолжает прежнюю сборку.
        db.execute('DELETE FROM cargo_orders WHERE account_id=? AND order_id=?', (account['id'], order_id))


def before_ready(account: dict, user: dict, order_id: str):
    saved = cargo.get(account['id'], order_id)
    if not saved or saved['status'] in {'confirmed', 'baseline'}:
        return
    marks = marking.order(account['id'], order_id)
    cargo.apply(account, order_id, user, saved['boxes'], marking.codes(marks, full=True) if marks else [])


def label(account: dict, order_id: str, box_id: str) -> bytes:
    row = db.query_one('SELECT campaign_id,shipment_id FROM yandex_orders WHERE account_id=? AND id=?',
                       (account['id'], order_id))
    if not row:
        raise ValueError('Заказ не найден')
    if box_id == f'order:{order_id}':
        return client.get_client(account).order_labels(str(row['campaign_id']), order_id, fmt=client.label_format())
    return client.get_client(account).box_label(str(row['campaign_id']), order_id, box_id,
                                               shipment_id=str(row['shipment_id'] or 0))


def refresh_orders(account: dict, order_ids: set[str]):
    """Список заказов может не содержать boxesLayout: подтверждаем отдельным чтением."""
    for row in db.query('SELECT order_id FROM cargo_orders WHERE account_id=? AND status IN (\'confirmed\',\'baseline\')',
                        (account['id'],)):
        if row['order_id'] not in order_ids:
            continue
        try:
            cargo.refresh(account, row['order_id'])
        except (ValueError, client.YandexError) as exc:
            # Отмена сборки и синхронизация остаются доступны при временном отказе API.
            log.warning('Раскладка Маркета не обновлена: кабинет %s, заказ %s, %s',
                        account['id'], row['order_id'], type(exc).__name__)


SOURCE = CargoSource(read, write, label, prepare_ready, before_ready)
