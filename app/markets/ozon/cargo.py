"""Разделение Ozon и перенос ЧЗ по фактическим экземплярам новых отправлений."""
from __future__ import annotations

import json
from collections import Counter, defaultdict

from ...core import cargo, db, exemplar_data, marking, marking_codes, marking_registry, pack_state
from ...core.config import settings
from . import client, store


def packages(items: list[dict]) -> list[list[dict]]:
    rows = [{'product_id': int(i['sku']), 'quantity': int(i['quantity'])} for i in items]
    if any(i['quantity'] < 1 for i in rows):
        raise ValueError('Некорректное количество товара')
    if not settings.split_orders_on_pack or sum(i['quantity'] for i in rows) < 2:
        return [rows]
    if sum(i['quantity'] for i in rows) > cargo.MAX_BOXES:
        raise ValueError(f'За один раз можно разделить не больше {cargo.MAX_BOXES} единиц')
    return [[{'product_id': i['product_id'], 'quantity': 1}] for i in rows for _ in range(i['quantity'])]


def children(account_id: int, parent: str) -> list[dict]:
    return [dict(r) for r in db.query('SELECT * FROM cargo_split_children WHERE account_id=? AND parent_id=? '
                                     'ORDER BY child_id', (account_id, parent))]


def intend(account: dict, parent: str, items: list[dict], user: dict):
    db.execute('INSERT OR IGNORE INTO cargo_split_intents VALUES(?,?,?,?,?)',
               (account['id'], parent, json.dumps(items), user['id'], db.now_iso()))


def ship(account: dict, user: dict, parent: str, items: list[dict], boxes: list[list[dict]], *, retry_confirmed=False):
    from ...core import readiness
    from .client import OzonError
    if len(boxes) > 1:
        intend(account, parent, items, user)
    try:
        return readiness.attempt(account, parent, lambda: client.get_client(account).ship(parent, boxes),
                                 retry_confirmed=retry_confirmed)
    except OzonError as exc:
        if exc.status in {400, 401, 403, 404, 409, 422, 429}:
            db.execute('DELETE FROM cargo_split_intents WHERE account_id=? AND parent_id=?', (account['id'], parent))
        raise


def recover(account: dict, parent: str, user: dict):
    intent = db.query_one('SELECT * FROM cargo_split_intents WHERE account_id=? AND parent_id=?', (account['id'], parent))
    if not intent or children(account['id'], parent):
        return
    request = db.query_one('SELECT status FROM ready_requests WHERE account_id=? AND order_id=?', (account['id'], parent))
    if not request or request['status'] != 'uncertain':
        db.execute('DELETE FROM cargo_split_intents WHERE account_id=? AND parent_id=?', (account['id'], parent))
        return
    raw = client.get_client(account).posting_get(parent)
    if (raw or {}).get('substatus') == 'ship_failed':
        db.execute('DELETE FROM cargo_split_intents WHERE account_id=? AND parent_id=?', (account['id'], parent))
        return
    numbers = ((raw or {}).get('related_postings') or {}).get('related_posting_numbers') or []
    if not numbers:
        raise ValueError('Ozon пока не подтвердил номера новых отправлений. Повторный запрос сборки не отправлен.')
    if raw.get('status') == store.STATUS_AWAITING_DELIVER and parent not in numbers:
        numbers = [parent, *numbers]
    remember(account, parent, numbers, json.loads(intent['source_items']), user, force=True)


def remember(account: dict, parent: str, numbers: list[str], items: list[dict], user: dict, *, force=False):
    if len(numbers) == 1 and numbers[0] == parent and not force:
        return
    if not numbers or len(set(numbers)) != len(numbers) or any(not isinstance(n, str) or not n for n in numbers):
        raise ValueError('Ozon вернул некорректные номера отправлений')
    state = db.query_one('SELECT scanned FROM pack_state WHERE account_id=? AND posting_number=? AND user_id=?',
                         (account['id'], parent, user['id']))
    scans = state['scanned'] if state else '{}'
    marked = marking.order(account['id'], parent)
    marks_verified = 0 if marking.needs_validation(marked) else 1
    with db.write() as conn:
        conn.executemany('INSERT OR IGNORE INTO cargo_split_children'
                         '(account_id,parent_id,child_id,source_items,source_scanned,marks_verified,user_id,created_at) VALUES(?,?,?,?,?,?,?,?)',
                         [(account['id'], parent, number, json.dumps(items), scans, marks_verified, user['id'], db.now_iso()) for number in numbers])


def _verified_rows(account: dict, parent: str) -> tuple[list[dict], list[dict]]:
    saved = children(account['id'], parent)
    if not saved:
        return [], []
    expected = Counter({str(i['sku']): int(i['quantity']) for i in json.loads(saved[0]['source_items'])})
    if settings.split_orders_on_pack and sum(expected.values()) > 1 and len(saved) != sum(expected.values()):
        raise ValueError('Ozon пока не подтвердил отдельное отправление на каждую единицу')
    rows, actual = [], Counter()
    for child in saved:
        raw = client.get_client(account).posting_get(child['child_id'])
        if not raw or str(raw.get('posting_number')) != child['child_id']:
            raise ValueError('Ozon пока не подтвердил новые отправления. Повторите проверку.')
        if raw.get('status') != store.STATUS_AWAITING_DELIVER or raw.get('substatus') == 'ship_failed':
            raise ValueError('Ozon пока не подтвердил готовность всех новых отправлений')
        for item in raw.get('products') or []:
            actual[str(item['sku'])] += int(item['quantity'])
        rows.append(raw)
    if actual != expected:
        raise ValueError('Ozon не подтвердил полный состав разделённого заказа')
    return saved, rows


def verify(account: dict, parent: str) -> list[str]:
    saved, rows = _verified_rows(account, parent)
    if not saved:
        return []
    progress = defaultdict(int)
    scanned = json.loads(saved[0]['source_scanned'])
    with db.write() as conn:
        for raw in rows:
            store.upsert_posting(conn, account['id'], raw)
            inherited = {}
            for item in raw['products']:
                sku, quantity = str(item['sku']), int(item['quantity'])
                inherited[sku] = min(quantity, max(0, int(scanned.get(sku, 0)) - progress[sku]))
                progress[sku] += quantity
            conn.execute('UPDATE cargo_split_children SET verified=1,scanned=? WHERE account_id=? AND parent_id=? AND child_id=?',
                         (json.dumps(inherited), account['id'], parent, str(raw['posting_number'])))
        numbers = [str(r['posting_number']) for r in rows]
        if parent not in numbers:
            conn.execute("UPDATE postings SET status='cancelled',claim_user_id=NULL,claim_login=NULL,claim_at=NULL "
                         'WHERE account_id=? AND posting_number=?', (account['id'], parent))
    for raw in rows:
        number = str(raw['posting_number'])
        snapshot = {'products': [{'key': str(i['sku']), 'sku': str(i['sku']), 'quantity': int(i['quantity'])}
                                 for i in raw['products']]}
        plan = cargo.create(account, number, snapshot, {'id': saved[0]['user_id']})
        if plan['status'] != 'confirmed':
            cargo._install(account, number, [{'id': 'box-1', 'units': [u['id'] for u in plan['units']],
                            'remote_id': number, 'barcode': str((raw.get('barcodes') or {}).get('upper_barcode') or number)}])
    return numbers


def _assign(context: dict, by_code: dict, found: set) -> list[dict]:
    assigned = []
    for product in context['products']:
        for unit, exemplar in enumerate(product['exemplars'], 1):
            for mark in exemplar.get('marks') or []:
                value = mark.get('mark')
                if mark.get('mark_type') != 'mandatory_mark':
                    continue
                match = next((v for v in by_code if marking_codes.equivalent(v, value)), None)
                if match is None:
                    continue
                original = by_code[match]
                if match in found or original['item_key'] != str(product['product_id']):
                    raise ValueError('Ozon вернул неоднозначную привязку Честный знак к отправлениям')
                found.add(match)
                assigned.append({**original, 'source_unit': original['unit_no'],
                                 'item_key': str(product['product_id']), 'unit_no': unit})
    return assigned


def _mapped(account: dict, parent: str, codes: list[dict]) -> list[dict]:
    from . import marking as provider
    by_code = {c['raw']: c for c in codes}
    saved = marking.order(account['id'], parent)
    details = exemplar_data.values(saved)
    policies = {p['key']: p for p in marking.products(saved)} if saved else {}
    original_context = exemplar_data.context(saved) if saved else {}
    original_units = {(str(p['product_id']), str(e['exemplar_id'])): unit
                      for p in original_context.get('products', []) for unit, e in enumerate(p['exemplars'], 1)}
    data_found = set()
    output, found = [], set()
    for child in children(account['id'], parent):
        number = child['child_id']
        local = {str(i['sku']): int(i['quantity']) for i in store.posting_items(account['id'], number)}
        relevant = set(local).intersection({c['item_key'] for c in codes} | set(details))
        if not relevant:
            continue
        required = {k for k in relevant if policies.get(k, {}).get('required')}
        data = provider._exemplars(account, number, local, required)
        assigned = _assign(data['context'], by_code, found)
        if required and not assigned:
            raise ValueError('Ozon пока не подтвердил Честный знак новых отправлений')
        child_details = _child_details(data, policies, original_units, details, assigned, data_found)
        data['details'] = child_details
        data['context']['_panel_details'] = child_details
        if provider.poll(account, number, data['context'], assigned)['status'] != 'accepted':
            raise ValueError('Дождитесь проверки кодов «Честный знак» новых отправлений в Ozon')
        provider.finalize(account, number, data['context'], assigned)
        output.append({'order_id': number, 'data': data, 'codes': assigned})
    if found != set(by_code):
        raise ValueError('Не все сохранённые коды «Честный знак» подтверждены в новых отправлениях Ozon')
    if data_found != {(k, int(u)) for k, rows in details.items() for u, values in rows.items() if values}:
        raise ValueError('Не все дополнительные данные подтверждены в новых отправлениях Ozon')
    return output


def _child_details(data, policies, original_units, details, assigned, data_found):
    child_details = {}
    for product in data['context']['products']:
        key = str(product['product_id'])
        policy = policies.get(key, {})
        metadata = next(p for p in data['products'] if p['key'] == key)
        metadata['fields'] = sorted(set(metadata.get('fields', [])) | set(policy.get('fields', [])))
        for unit, e in enumerate(product['exemplars'], 1):
            source_unit = original_units.get((key, str(e['exemplar_id'])))
            if source_unit is None:
                source_unit = next((m['source_unit'] for m in assigned if m['item_key'] == key and m['unit_no'] == unit), None)
            if key in details and source_unit is None:
                raise ValueError('Ozon пока не подтвердил привязку дополнительных данных к новым экземплярам')
            values = exemplar_data.at(details, key, source_unit) if source_unit else {}
            if values:
                if (key, source_unit) in data_found:
                    raise ValueError('Ozon вернул повторяющийся экземпляр дополнительных данных')
                data_found.add((key, source_unit))
                child_details.setdefault(key, {})[str(unit)] = values
    return child_details


def _move_reports(conn, account_id: int, parent: str, mapped: list[dict]):
    moves = []
    for child in mapped:
        for mark in child['codes']:
            row = conn.execute('SELECT id FROM shipped_items WHERE account_id=? AND posting_number=? '
                               "AND item_key=? AND unit_no=? AND status<>'error'",
                               (account_id, parent, mark['item_key'], mark['source_unit'])).fetchone()
            if row:
                moves.append((child['order_id'], mark['unit_no'], row['id']))
    # Освобождаем исходные слоты в той же транзакции: номер родителя может сохраниться.
    conn.executemany('UPDATE shipped_items SET unit_no=-id WHERE id=?', [(row_id,) for _, _, row_id in moves])
    conn.executemany('UPDATE shipped_items SET posting_number=?,unit_no=? WHERE id=?', moves)


def _save_marks(account: dict, parent: str, mapped: list[dict], codes: list[dict], user: dict):
    now = db.now_iso()
    with db.write() as conn:
        _move_reports(conn, account['id'], parent, mapped)
        # У одного из детей может сохраниться номер родителя. Сначала освобождаем слоты единиц.
        conn.executemany('UPDATE marking_codes SET active=0 WHERE id=?', [(m['id'],) for m in codes])
        for child in mapped:
            number, data = child['order_id'], child['data']
            conn.execute('INSERT OR IGNORE INTO sticker_orders(account_id,marketplace,shop_title,order_id,order_number,created_at) '
                         'VALUES(?,?,?,?,?,?)', (account['id'], account['marketplace'], account['title'], number, number, now))
            archive_id = conn.execute('SELECT id FROM sticker_orders WHERE account_id=? AND order_id=?', (account['id'], number)).fetchone()['id']
            conn.execute('INSERT INTO marking_orders(account_id,order_id,archive_id,products,context,status,user_id,created_at,checked_at) '
                         "VALUES(?,?,?,?,?,'accepted',?,?,?) ON CONFLICT(account_id,order_id) DO UPDATE SET "
                         "products=excluded.products,context=excluded.context,status='accepted',error=NULL,checked_at=excluded.checked_at",
                         (account['id'], number, archive_id, json.dumps(data['products']),
                          marking._seal(json.dumps(data['context'], ensure_ascii=False)), user['id'], now, now))
            mark_id = conn.execute('SELECT id FROM marking_orders WHERE account_id=? AND order_id=?', (account['id'], number)).fetchone()['id']
            conn.execute("UPDATE marking_orders SET details=?,field_errors='{}',user_id=? WHERE id=?",
                         (marking._seal(json.dumps(data.get('details') or {}, ensure_ascii=False)), user['id'], mark_id))
            for mark in child['codes']:
                conn.execute("UPDATE marking_codes SET marking_order_id=?,item_key=?,unit_no=?,active=1,status='accepted',error=NULL WHERE id=?",
                             (mark_id, mark['item_key'], mark['unit_no'], mark['id']))
                marking_registry.binding(mark['fingerprint'], conn)
                conn.execute('UPDATE marking_registry SET order_id=?,revision=revision+1,updated_at=? WHERE fingerprint=? AND code_id=?',
                             (number, now, mark['fingerprint'], mark['id']))
            progress = cargo.seed_scans(account['id'], number)
            for mark in child['codes']:
                progress[mark['item_key']] = max(progress.get(mark['item_key'], 0), mark['unit_no'])
            conn.execute('UPDATE cargo_split_children SET scanned=? WHERE account_id=? AND parent_id=? AND child_id=?',
                         (json.dumps(progress), account['id'], parent, number))
        conn.execute("UPDATE marking_orders SET status='accepted',error=NULL,checked_at=? WHERE account_id=? AND order_id=?", (now, account['id'], parent))
        conn.execute('UPDATE cargo_split_children SET marks_verified=1 WHERE account_id=? AND parent_id=?', (account['id'], parent))


def redistribute(account: dict, user: dict, parent: str, codes: list[dict]) -> bool:
    recover(account, parent, user)
    rows = children(account['id'], parent)
    if not rows or all(row['marks_verified'] for row in rows):
        # Completed redistribution is historical. A later marking change
        # belongs to the current child, even if it kept its parent's number.
        return False
    verify(account, parent)
    mapped = _mapped(account, parent, codes)
    _save_marks(account, parent, mapped, codes, user)
    active = db.query_one('SELECT 1 FROM pack_state WHERE account_id=? AND user_id=? AND posting_number=?',
                          (account['id'], user['id'], parent))
    if active:
        selected = mapped[0]['order_id'] if mapped else rows[0]['child_id']
        with db.write() as conn:
            pack_state.release_previous(conn, user, keep=(account['id'], selected))
            pack_state.claim(conn, account, user, selected)
            pack_state.save(conn, account, user, selected, cargo.seed_scans(account['id'], selected))
    db.log_event('cargo_split', account_id=account['id'], user=user, posting_number=parent,
                 message='Ozon подтвердил отправления и Честный знак: ' + ', '.join(r['child_id'] for r in rows))
    return True
