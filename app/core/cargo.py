"""Грузоместа: полная раскладка единиц, подтверждение площадкой и ярлыки.

Единица опознаётся парой (позиция заказа, номер единицы), а не строкой UI.
ЧЗ в раскладке не дублируется в БД; исходные коды остаются в marking_codes.
"""
from __future__ import annotations

import hashlib
import io
import json
import re

from . import db, marking, order_lock

MAX_BOXES = 50
SCHEMA = """
CREATE TABLE IF NOT EXISTS cargo_orders (
 account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 order_id TEXT NOT NULL, units TEXT NOT NULL, boxes TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'draft',
 pending TEXT, checked TEXT NOT NULL DEFAULT '[]', error TEXT, remote_stamp TEXT,
 user_id INTEGER, updated_at TEXT NOT NULL,
 PRIMARY KEY(account_id,order_id)
);
CREATE TABLE IF NOT EXISTS cargo_labels (
 account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 order_id TEXT NOT NULL, box_id TEXT NOT NULL, signature TEXT NOT NULL,
 pdf BLOB NOT NULL, saved_at TEXT NOT NULL,
 PRIMARY KEY(account_id,order_id,box_id)
);
CREATE TABLE IF NOT EXISTS cargo_barcodes (
 account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 order_id TEXT NOT NULL, barcode TEXT NOT NULL, box_id TEXT NOT NULL,
 PRIMARY KEY(account_id,barcode)
);
CREATE TABLE IF NOT EXISTS cargo_split_children (
 account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 parent_id TEXT NOT NULL, child_id TEXT NOT NULL, scanned TEXT NOT NULL DEFAULT '{}',
 source_items TEXT NOT NULL DEFAULT '[]', source_scanned TEXT NOT NULL DEFAULT '{}',
 verified INTEGER NOT NULL DEFAULT 0, marks_verified INTEGER NOT NULL DEFAULT 1,
 user_id INTEGER, created_at TEXT NOT NULL,
 PRIMARY KEY(account_id,parent_id,child_id)
);
CREATE TABLE IF NOT EXISTS cargo_split_intents (
 account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 parent_id TEXT NOT NULL, source_items TEXT NOT NULL, user_id INTEGER NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(account_id,parent_id)
);
"""


def source(account: dict):
    from ..markets import registry
    market = registry.get(account['marketplace'])
    return market.cargo if market else None


def get(account_id: int, order_id: str) -> dict | None:
    row = db.query_one('SELECT * FROM cargo_orders WHERE account_id=? AND order_id=?', (account_id, order_id))
    if not row:
        return None
    result = dict(row)
    for key in ('units', 'boxes', 'checked'):
        result[key] = json.loads(result[key])
    result['pending'] = json.loads(result['pending']) if result['pending'] else None
    return result


def token(key: str, unit: int) -> str:
    return hashlib.sha256(f'{key}\0{unit}'.encode()).hexdigest()[:24]


def units(products: list[dict]) -> list[dict]:
    if not products or sum(int(p['quantity']) for p in products) > 500:
        raise ValueError('Не удалось безопасно получить состав заказа')
    result = []
    for product in products:
        key = str(product['key'])
        quantity = int(product['quantity'])
        if not key or not 1 <= quantity <= 500:
            raise ValueError('Некорректное количество товара')
        result += [{'id': token(key, n), 'key': key, 'sku': str(product['sku']), 'unit': n}
                   for n in range(1, quantity + 1)]
    if len({u['id'] for u in result}) != len(result):
        raise ValueError('Повторяющаяся позиция заказа')
    return result


def default_boxes(items: list[dict], *, separate: bool = False) -> list[dict]:
    groups = [[u['id']] for u in items] if separate else [[u['id'] for u in items]]
    if len(groups) > MAX_BOXES:
        raise ValueError(f'За один раз можно сформировать не больше {MAX_BOXES} грузомест')
    return [{'id': f'box-{n}', 'units': group} for n, group in enumerate(groups, 1)]


def _shape(items: list[dict]):
    return {(u['key'], u['sku'], u['unit']) for u in items}


def create(account: dict, order_id: str, snapshot: dict, user: dict, *, separate: bool = False) -> dict:
    items = units(snapshot['products'])
    saved = get(account['id'], order_id)
    if saved:
        if _shape(saved['units']) != _shape(items):
            raise ValueError('Состав заказа изменился. Перечитайте заказ перед распределением.')
        return saved
    boxes = snapshot.get('boxes') or default_boxes(items, separate=separate)
    status = ('confirmed' if all(b.get('remote_id') and b.get('barcode') for b in boxes)
              else 'baseline' if snapshot.get('baseline') else 'draft')
    db.execute('INSERT OR IGNORE INTO cargo_orders(account_id,order_id,units,boxes,status,user_id,updated_at,remote_stamp) '
               'VALUES(?,?,?,?,?,?,?,?)', (account['id'], order_id, json.dumps(items), json.dumps(boxes),
                                         status, user['id'], db.now_iso(), snapshot.get('remote_stamp')))
    result = get(account['id'], order_id)
    if result['status'] == 'confirmed':
        with db.write() as conn:
            _aliases(conn, account['id'], order_id, boxes)
    return result


def _aliases(conn, account_id: int, order_id: str, boxes: list[dict]):
    conn.execute('DELETE FROM cargo_barcodes WHERE account_id=? AND order_id=?', (account_id, order_id))
    conn.executemany('INSERT INTO cargo_barcodes VALUES(?,?,?,?)',
                     [(account_id, order_id, b['barcode'], str(b['remote_id'])) for b in boxes if b.get('barcode')])


def label_order(account_id: int, barcode: str) -> str | None:
    row = db.query_one('SELECT order_id FROM cargo_barcodes WHERE account_id=? AND barcode=?', (account_id, barcode))
    return row['order_id'] if row else None


def validate(saved: dict, wanted) -> list[dict]:
    if not isinstance(wanted, list) or not 1 <= len(wanted) <= MAX_BOXES:
        raise ValueError(f'Укажите от 1 до {MAX_BOXES} грузомест')
    known = {u['id'] for u in saved['units']}
    previous = {b['id']: b for b in saved['boxes']}
    result, seen = [], set()
    for box in wanted:
        if not isinstance(box, dict) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', str(box.get('id') or '')):
            raise ValueError('Некорректное грузоместо')
        key, members = box['id'], box.get('units')
        if key in seen or not isinstance(members, list) or len(members) > 500:
            raise ValueError('Повторяющееся или некорректное грузоместо')
        seen.add(key)
        if any(not isinstance(u, str) or u not in known for u in members) or len(set(members)) != len(members):
            raise ValueError('Неизвестная или повторяющаяся единица товара')
        if members:
            result.append({**previous.get(key, {}), 'id': key, 'units': list(members)})
    if not result or set().union(*(set(b['units']) for b in result)) != known:
        raise ValueError('Распределите все товары, не меняя их количество')
    shared = {u for u in known if sum(u in b['units'] for b in result) > 1}
    if any(len(b['units']) != 1 and shared.intersection(b['units']) for b in result):
        raise ValueError('Часть одного товара должна ехать отдельно от остальных товаров')
    return result


def _active(account: dict, order_id: str, user: dict):
    row = db.query_one('SELECT 1 FROM pack_state WHERE user_id=? AND account_id=? AND posting_number=?',
                       (user['id'], account['id'], order_id))
    if not row:
        raise ValueError('Этот заказ не открыт у вас в сборке')


def open_editor(account: dict, order_id: str, user: dict) -> dict:
    _active(account, order_id, user)
    provider = source(account)
    if not provider:
        raise ValueError('Для этой площадки ручное добавление грузомест недоступно')
    with order_lock.hold(account['id'], order_id):
        _inflight_marking(account, order_id)
        snapshot = provider.read(account, order_id)
        create(account, order_id, snapshot, user)
        return reconcile(account, order_id, snapshot)


def reconcile(account: dict, order_id: str, snapshot: dict) -> dict | None:
    """Авторитетный ответ площадки заменяет только подтверждённую раскладку.

    Пустая раскладка — тоже изменение. ЧЗ и первичный PDF архива сохраняются;
    текущий комплект ярлыков будет заново загружен по номеру всего заказа.
    Черновик и неопределённая запись требуют своего процесса подтверждения.
    Вызывается под order_lock.
    """
    saved = get(account['id'], order_id)
    if not saved or saved['status'] not in {'confirmed', 'baseline'}:
        return saved
    if _shape(saved['units']) != _shape(units(snapshot['products'])):
        raise ValueError('Состав заказа изменился. Перечитайте заказ перед распределением.')
    stamp = snapshot.get('remote_stamp')
    if stamp is None or saved['remote_stamp'] == stamp:
        return saved
    boxes = snapshot.get('boxes')
    if boxes:
        validate(saved, boxes)
        if (not all(b.get('remote_id') and b.get('barcode') for b in boxes)
                or len({b['remote_id'] for b in boxes}) != len(boxes)
                or len({b['barcode'] for b in boxes}) != len(boxes)):
            raise ValueError('Маркет пока не подтвердил идентификаторы и ярлыки грузомест')
        return _install(account, order_id, boxes, stamp)
    boxes = default_boxes(saved['units'])
    boxes[0].update(remote_id=f'order:{order_id}', barcode='')
    return _install(account, order_id, boxes, stamp, status='baseline')


def refresh(account: dict, order_id: str) -> dict | None:
    saved = get(account['id'], order_id)
    provider = source(account)
    if not provider or not saved or saved['status'] not in {'confirmed', 'baseline'}:
        return saved
    with order_lock.hold(account['id'], order_id):
        return reconcile(account, order_id, provider.read(account, order_id))


def _inflight_marking(account: dict, order_id: str):
    saved = marking.order(account['id'], order_id)
    if saved and saved['status'] in {'queued', 'checking', 'uncertain'}:
        raise ValueError('Дождитесь проверки ЧЗ перед изменением грузомест')


def save_editor(account: dict, order_id: str, user: dict, revision, boxes):
    _active(account, order_id, user)
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise ValueError('Перечитайте раскладку заказа')
    with order_lock.hold(account['id'], order_id):
        _inflight_marking(account, order_id)
        saved = get(account['id'], order_id)
        if not saved or saved['revision'] != revision:
            raise ValueError('Раскладка уже изменилась. Перечитайте заказ.')
        wanted = validate(saved, boxes)
        if saved['status'] in {'confirmed', 'baseline'} and [(b['id'], sorted(b['units'])) for b in wanted] == [
            (b['id'], sorted(b['units'])) for b in saved['boxes']]:
            return saved
        marks_order = marking.order(account['id'], order_id)
        marks = marking.codes(marks_order, full=True) if marks_order else []
        # Пока обязательные ЧЗ не собраны, сохраняем намерение, не отправляя неполные экземпляры.
        required = marking.has_required(marks_order)
        required_count = sum(p['quantity'] for p in marking.products(marks_order) if p.get('required')) if required else 0
        if required and len(marks) < required_count:
            db.execute('UPDATE cargo_orders SET boxes=?,revision=revision+1,status=\'draft\',error=NULL,updated_at=? '
                       'WHERE account_id=? AND order_id=?', (json.dumps(wanted), db.now_iso(), account['id'], order_id))
            return get(account['id'], order_id)
        return apply(account, order_id, user, wanted, marks)


def _install(account: dict, order_id: str, boxes: list[dict], remote_stamp=None, *, status='confirmed') -> dict:
    with db.write() as conn:
        conn.execute("UPDATE cargo_orders SET boxes=?,revision=revision+1,status=?,pending=NULL,"
                     "checked='[]',error=NULL,updated_at=?,remote_stamp=? WHERE account_id=? AND order_id=?",
                     (json.dumps(boxes), status, db.now_iso(), remote_stamp, account['id'], order_id))
        # После новой ревизии нужен актуальный комплект, даже если площадка сохранила
        # старые номера мест. Формат ключа кэша прежних версий при этом не меняется.
        _aliases(conn, account['id'], order_id, boxes)
        conn.execute('DELETE FROM cargo_labels WHERE account_id=? AND order_id=?', (account['id'], order_id))
    return get(account['id'], order_id)


def apply(account: dict, order_id: str, user: dict, wanted: list[dict], marks: list[dict], dispatch=lambda: None) -> dict:
    provider = source(account)
    saved = get(account['id'], order_id)
    if not provider or not saved:
        raise ValueError('Раскладка заказа не подготовлена')
    wanted = validate(saved, wanted)
    uncertain = saved['status'] == 'uncertain'
    if uncertain:
        wanted = saved['pending'] or wanted
    sent = False

    def sending():
        nonlocal sent
        if uncertain:
            raise ValueError('Маркет пока не подтвердил предыдущую раскладку. Повторный запрос не отправлен.')
        dispatch()
        db.execute("UPDATE cargo_orders SET status='uncertain',pending=?,error=NULL,updated_at=? "
                   'WHERE account_id=? AND order_id=?', (json.dumps(wanted), db.now_iso(), account['id'], order_id))
        sent = True

    try:
        confirmed = provider.write(account, user, order_id, saved['units'], wanted, marks, sending, uncertain)
    except Exception as exc:
        rejected = getattr(exc, 'status', None) in {400, 401, 403, 404, 409, 422, 429}
        if sent and rejected:
            db.execute('UPDATE cargo_orders SET status=?,pending=NULL,error=? WHERE account_id=? AND order_id=?',
                       (saved['status'], 'Маркет отклонил изменение раскладки', account['id'], order_id))
        elif sent or uncertain:
            db.execute('UPDATE cargo_orders SET error=? WHERE account_id=? AND order_id=?',
                       ('Площадка пока не подтвердила раскладку. Требуется повторная проверка.', account['id'], order_id))
        raise
    result = _install(account, order_id, confirmed['boxes'], confirmed.get('remote_stamp'))
    db.log_event('cargo_saved', account_id=account['id'], user=user, posting_number=order_id,
                 message=f'Площадка подтвердила {len(confirmed["boxes"])} грузомест')
    return result


def public(saved: dict) -> dict:
    return {key: saved[key] for key in ('units', 'boxes', 'revision', 'status', 'checked', 'error')}


def enrich(account: dict, state: dict) -> dict:
    active = state.get('active') or {}
    order_id = str(active.get('posting_number') or active.get('id') or '')
    if not order_id or not source(account):
        return state
    saved = get(account['id'], order_id)
    state['cargo'] = public(saved) if saved else {'boxes': [], 'units': [], 'revision': 0, 'status': 'empty', 'checked': [], 'error': None}
    marked = marking.order(account['id'], order_id)
    if saved and marked:
        metadata = {p['key']: p for p in marking.products(marked)}
        codes = {(m['item_key'], m['unit_no']): m for m in marking.codes(marked)}
        state['cargo']['units'] = []
        for original in saved['units']:
            unit = dict(original)
            mark = codes.get((unit['key'], unit['unit']))
            if mark or metadata.get(unit['key'], {}).get('required'):
                status = 'error' if mark and mark['status'] == 'error' else (
                    'accepted' if mark and marked['status'] == 'accepted' else 'waiting')
                unit['marking'] = {'key': unit['key'], 'status': status,
                                   'title': (mark.get('error') if mark else None) or
                                   ('ЧЗ принят площадкой' if status == 'accepted' else 'ЧЗ не отсканирован или проверяется')}
            state['cargo']['units'].append(unit)
    state['cargo']['editable'] = active.get('local_state') != 'packed'
    if saved and saved['status'] == 'uncertain':
        state['complete'] = False
    return state


def label_signature(saved: dict, box: dict) -> str:
    shape = [(b['id'], sorted(b['units'])) for b in saved['boxes']]
    return hashlib.sha256(json.dumps([shape, box['id'], box.get('barcode')], sort_keys=True).encode()).hexdigest()


def needs_labels(account_id: int, order_id: str) -> bool:
    saved = get(account_id, order_id)
    return bool(saved and (len(saved['boxes']) > 1 or saved['revision'] > 0))


def labels_allowed(account_id: int, order_id: str):
    if split_unknown(account_id, order_id):
        raise ValueError('Ozon ещё не подтвердил номера разделённых отправлений')
    if split_pending(account_id, order_id):
        raise ValueError('Ozon ещё не подтвердил перенос ЧЗ в новые отправления')
    saved = get(account_id, order_id)
    if saved and saved['status'] in {'draft', 'uncertain'}:
        raise ValueError('Сначала сохраните грузоместа и дождитесь подтверждения площадки')


def cached_pdf(account_id: int, order_id: str) -> bytes | None:
    saved = get(account_id, order_id)
    if not saved or not needs_labels(account_id, order_id):
        return None
    labels_allowed(account_id, order_id)
    from . import sticker_archive
    files = []
    for box in saved['boxes']:
        row = db.query_one('SELECT pdf FROM cargo_labels WHERE account_id=? AND order_id=? AND box_id=? '
                           'AND signature=? AND saved_at>=?', (account_id, order_id, str(box['remote_id']),
                                                               label_signature(saved, box), sticker_archive.cutoff()))
        if not row:
            raise ValueError('Актуальные ярлыки грузомест ещё загружаются')
        files.append(bytes(row['pdf']))
        if sum(len(pdf) for pdf in files) > sticker_archive.MAX_PDF_BYTES:
            raise ValueError('Комплект ярлыков превышает 20 МБ')
    if len(files) == 1:
        return files[0]
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter()
    for pdf in files:
        writer.append(PdfReader(io.BytesIO(pdf)))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def ensure_labels(account: dict, order_id: str) -> bytes:
    from . import assembly_stickers, sticker_archive
    with order_lock.hold(account['id'], order_id):
        refresh(account, order_id)
        labels_allowed(account['id'], order_id)
        saved = get(account['id'], order_id)
        if not saved:
            raise ValueError('Грузоместа не найдены')
        for box in saved['boxes']:
            signature = label_signature(saved, box)
            old = db.query_one('SELECT 1 FROM cargo_labels WHERE account_id=? AND order_id=? AND box_id=? AND signature=? AND saved_at>=?',
                               (account['id'], order_id, str(box['remote_id']), signature, sticker_archive.cutoff()))
            if old:
                continue
            provider = source(account)
            if provider:
                pdf = provider.label(account, order_id, str(box['remote_id']))
            else:
                from ..markets import registry
                got = registry.get(account['marketplace']).labels.pdf(account, {}, [order_id])
                pdf, missing = got if isinstance(got, tuple) else (got, [])
                if order_id in missing:
                    pdf = None
            assembly_stickers._validate(pdf)
            db.execute('INSERT INTO cargo_labels VALUES(?,?,?,?,?,?) ON CONFLICT(account_id,order_id,box_id) '
                       'DO UPDATE SET signature=excluded.signature,pdf=excluded.pdf,saved_at=excluded.saved_at',
                       (account['id'], order_id, str(box['remote_id']), signature, pdf, db.now_iso()))
        return cached_pdf(account['id'], order_id)


def complete_guard(account: dict, order_id: str):
    saved = refresh(account, order_id)
    if not saved:
        return
    labels_allowed(account['id'], order_id)
    if len(saved['boxes']) > 1 and set(saved['checked']) != {b['barcode'] for b in saved['boxes']}:
        raise ValueError('Отсканируйте ярлыки всех грузомест')


def scan_label(account: dict, user: dict, code: str, state: dict) -> dict | None:
    active = state.get('active') or {}
    order_id = str(active.get('posting_number') or active.get('id') or '')
    saved = get(account['id'], order_id) if order_id else None
    if not saved or len(saved['boxes']) < 2:
        return None
    barcodes = {b.get('barcode') for b in saved['boxes']}
    if code not in barcodes:
        if code == order_id:
            raise ValueError('Номер заказа не подтверждает грузоместо. Сканируйте ярлык каждой коробки.')
        return None
    if not state.get('complete'):
        raise ValueError('Сначала отсканируйте товары и дождитесь проверки ЧЗ')
    labels_allowed(account['id'], order_id)
    checked = set(saved['checked'])
    duplicate = code in checked
    checked.add(code)
    db.execute('UPDATE cargo_orders SET checked=?,updated_at=? WHERE account_id=? AND order_id=?',
               (json.dumps(sorted(checked)), db.now_iso(), account['id'], order_id))
    db.log_event('cargo_scan', account_id=account['id'], user=user, posting_number=order_id, barcode=code,
                 message=f'Подтверждено грузомест {len(checked)} из {len(barcodes)}')
    return {'done': checked == barcodes, 'duplicate': duplicate,
            'message': ('Это грузоместо уже отсканировано' if duplicate else
                        f'Грузоместо подтверждено: {len(checked)} из {len(barcodes)}')}


def seed_scans(account_id: int, order_id: str) -> dict:
    row = db.query_one('SELECT scanned FROM cargo_split_children WHERE account_id=? AND child_id=?',
                       (account_id, order_id))
    return json.loads(row['scanned']) if row else {}


def split_pending(account_id: int, order_id: str) -> bool:
    return bool(db.query_one('SELECT 1 FROM cargo_split_children WHERE account_id=? '
                             'AND (parent_id=? OR child_id=?) AND marks_verified=0 LIMIT 1',
                             (account_id, order_id, order_id)))


def split_unknown(account_id: int, order_id: str) -> bool:
    return bool(db.query_one('SELECT 1 FROM cargo_split_intents i WHERE account_id=? AND parent_id=? '
                             'AND NOT EXISTS(SELECT 1 FROM cargo_split_children c '
                             'WHERE c.account_id=i.account_id AND c.parent_id=i.parent_id)', (account_id, order_id)))
