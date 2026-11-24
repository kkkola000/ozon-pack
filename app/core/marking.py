"""ЧЗ: привязка к единице заказа, стойкая очередь передачи и контроль результата.

В базу сначала попадает полный код, затем создаётся задача. Сеть никогда не
держит транзакцию SQLite. Запись и опрос одной ревизии сериализованы арендой
заказа; падение процесса не теряет коды и не превращает HTTP 200 в «принято».
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
from datetime import datetime, timedelta, timezone

from . import accounts, crypto, db, exemplar_data, marking_codes, marking_registry, marking_returns, order_lock, pack_state
from .config import settings

log = logging.getLogger(__name__)
KV_AUTO = 'print_marking_duplicates'
NOT_RECORDED = 'Не записан ЧЗ на маркете'
LOCAL_ONLY_MESSAGE = ('Честный знак нужно передавать в статусе заказа Ожидает сборки, '
                      'завершите эту сборку отсканировав стикер отправления')


class Rejected(ValueError):
    def __init__(self, errors: dict[str, str], message: str = 'Площадка отклонила данные экземпляров', *, checks=None):
        documented = next((c['message'] for c in (checks or {}).values()
                           if c.get('status') == 'INVALID' and c.get('message')), None)
        super().__init__(documented or message)
        self.errors = errors
        self.checks = checks


class AwaitingCheck(ValueError):
    def __init__(self, outcome: dict):
        self.checks = outcome.get('checks')
        message = next((c['message'] for c in (self.checks or {}).values()
                        if c.get('status') in {'FAILED', 'IN_PROGRESS', 'NOT_ON_VALIDATION'}), None)
        super().__init__(message or 'Маркет пока не подтвердил сохранение полных данных экземпляров и успешную проверку кодов «Честный знак»')


class NotRecorded(Rejected):
    """The full code is retained locally but is absent from a closed remote layout."""
    def __init__(self, errors: dict[str, str], *, local_completion: bool = False):
        super().__init__(errors, NOT_RECORDED)
        self.local_completion = local_completion
SCHEMA = order_lock.SCHEMA + marking_registry.SCHEMA + marking_returns.SCHEMA + """
CREATE TABLE IF NOT EXISTS marking_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    order_id TEXT NOT NULL,
    archive_id INTEGER NOT NULL REFERENCES sticker_orders(id) ON DELETE CASCADE,
    products TEXT NOT NULL,
    context TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    field_errors TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'collecting',
    revision INTEGER NOT NULL DEFAULT 0,
    submitted_revision INTEGER,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT,
    replacements_checked_at TEXT,
    rescan_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    user_id INTEGER,
    UNIQUE(account_id, order_id)
);
CREATE TABLE IF NOT EXISTS marking_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    marking_order_id INTEGER NOT NULL REFERENCES marking_orders(id) ON DELETE CASCADE,
    item_key TEXT NOT NULL,
    unit_no INTEGER NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    code TEXT NOT NULL,
    gtin TEXT NOT NULL,
    serial TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'saved',
    error TEXT,
    check_status TEXT,
    check_substatus TEXT,
    check_message TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_marking_unit ON marking_codes(marking_order_id,item_key,unit_no)
    WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_marking_pending ON marking_orders(status,checked_at);
CREATE INDEX IF NOT EXISTS idx_marking_replacement_due ON marking_orders(status,replacements_checked_at);
CREATE INDEX IF NOT EXISTS idx_marking_gtin ON marking_codes(gtin) WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_marking_order_history ON marking_codes(marking_order_id,active);
"""


def source(account: dict):
    from ..markets import registry

    market = registry.get(account['marketplace'])
    return market.marking if market else None


def enabled() -> bool:
    return db.kv_get(KV_AUTO, '0') == '1'


def _seal(value: str) -> str:
    # crypto.encrypt обрезает пробелы ключей: для бинарно-точных ЧЗ — base64.
    return crypto.encrypt(base64.b64encode(value.encode('utf-8')).decode('ascii'))


def _open(value: str) -> str:
    try:
        decrypted = crypto.decrypt(value)
        if not decrypted:
            raise ValueError('Не удалось расшифровать код')
        return base64.b64decode(decrypted, validate=True).decode('utf-8')
    except (ValueError, UnicodeError) as exc:
        raise ValueError('Не удалось расшифровать сохранённый код «Честный знак». Проверьте ключ шифрования панели.') from exc


def order(account_id: int, order_id: str) -> dict | None:
    row = db.query_one('SELECT * FROM marking_orders WHERE account_id = ? AND order_id = ?',
                       (account_id, order_id))
    return dict(row) if row else None


def prepare(account: dict, order_id: str, *, for_scan: bool = False) -> dict | None:
    provider = source(account)
    if not provider:
        return None
    saved = order(account['id'], order_id)
    # Вызывается под order_lock, чтобы не создать два несовместимых снимка.
    data = (provider.discover if for_scan and provider.discover else provider.prepare)(account, order_id)
    if data is None:
        if needs_validation(saved):
            raise ValueError('Не удалось подтвердить требования к уже сохранённым кодам «Честный знак» заказа')
        return None
    products = data['products']
    if not products or sum(int(p['quantity']) for p in products) > 500:
        raise ValueError('Не удалось безопасно получить состав экземпляров заказа')
    if any(not p.get('key') or not 1 <= int(p['quantity']) <= 500 for p in products):
        raise ValueError('Площадка вернула некорректный состав экземпляров')
    if saved:
        # The provider may have reconciled a confirmed remote deletion.
        saved = order(account['id'], order_id)
        return _refresh(saved, data)
    now = db.now_iso()
    with db.write() as conn:
        conn.execute(
            'INSERT OR IGNORE INTO sticker_orders(account_id,marketplace,shop_title,order_id,order_number,created_at) '
            'VALUES(?,?,?,?,?,?)', (account['id'], account['marketplace'], account['title'], order_id, order_id, now))
        archive_id = conn.execute('SELECT id FROM sticker_orders WHERE account_id = ? AND order_id = ?',
                                  (account['id'], order_id)).fetchone()['id']
        conn.execute(
            'INSERT OR IGNORE INTO marking_orders(account_id,order_id,archive_id,products,context,details,created_at) '
            'VALUES(?,?,?,?,?,?,?)', (account['id'], order_id, archive_id, json.dumps(products),
                                     _seal(json.dumps(data['context'], ensure_ascii=False)),
                                     _seal(json.dumps(data.get('details') or {}, ensure_ascii=False)), now))
    return order(account['id'], order_id)


def _refresh(saved: dict, data: dict) -> dict:
    products = data['products']
    def signature(rows):
        return {(str(p['key']), str(p['sku']), int(p['quantity'])) for p in rows}
    previous = json.loads(saved['products'])
    old_policy = {p['key']: p for p in previous}
    stricter = any(p.get(flag) and not old_policy.get(p['key'], {}).get(flag)
                   for p in products for flag in ('required', 'possible'))
    stricter = stricter or any(set(p.get('fields', [])) - set(old_policy.get(p['key'], {}).get('fields', []))
                              for p in products)
    # Площадка может снять требование после получения ЧЗ. Уже сохранённое
    # обязательство сохраняем; новый обязательный товар требует сверки.
    if signature(products) == signature(previous) and not stricter:
        return saved
    if signature(products) != signature(previous) and (codes(saved) or exemplar_data.values(saved)):
        db.execute("UPDATE marking_orders SET status='error',error=? WHERE id=?",
                   ('Состав или требования заказа изменились. Сверьте уже сохранённые коды «Честный знак» перед продолжением.', saved['id']))
        raise ValueError('Состав заказа изменился; сохранённые коды «Честный знак» требуют сверки')
    if saved['status'] in {'checking', 'uncertain'}:
        raise ValueError('Требования изменились во время передачи. Дождитесь проверки данных площадкой.')
    for product in products:
        old = old_policy.get(product['key'], {})
        product['required'] = bool(product.get('required') or old.get('required'))
        product['fields'] = sorted(set(product.get('fields', [])) | set(old.get('fields', [])))
    db.execute("UPDATE marking_orders SET products=?,context=?,status='collecting',revision=revision+1 WHERE id=?",
               (json.dumps(products), _seal(json.dumps(data['context'], ensure_ascii=False)), saved['id']))
    return order(saved['account_id'], saved['order_id'])


def products(saved: dict) -> list[dict]:
    rows = json.loads(saved['products'])
    for product in rows:
        product['fields'] = [f for f in product.get('fields', []) if f in exemplar_data.LABELS]
    return rows


def retire(account: dict, saved: dict, ids: list[int], context: dict) -> None:
    """Remove confirmed remote bindings from active work, retaining the audit row."""
    if not ids:
        return
    with order_lock.hold(account['id'], saved['order_id']), db.write() as conn:
        conn.executemany("UPDATE marking_codes SET active=0,status='removed',error=NULL "
                         "WHERE id=? AND marking_order_id=? AND active=1 AND status='accepted'",
                         [(code_id, saved['id']) for code_id in ids])
        conn.execute("UPDATE marking_orders SET context=?,status='collecting',revision=revision+1,"
                     'submitted_revision=NULL,error=NULL,checked_at=NULL WHERE id=?',
                     (_seal(json.dumps(context, ensure_ascii=False)), saved['id']))
        db.log_event('marking_removed_remotely', account_id=account['id'], posting_number=saved['order_id'],
                     message=f'На площадке снята маркировка: {len(ids)} экз.', conn=conn)


def has_required(saved: dict | None) -> bool:
    return bool(saved and any(p.get('required') for p in products(saved)))


def codes(saved: dict, *, full: bool = False, active: bool = True) -> list[dict]:
    rows = [dict(r) for r in db.query('SELECT * FROM marking_codes WHERE marking_order_id = ? '
                                    + ('AND active = 1 ' if active else '') + 'ORDER BY item_key,unit_no,id',
                                    (saved['id'],))]
    if full:
        for row in rows:
            row['raw'] = _open(row['code'])
    return rows


def _fingerprint(parsed: marking_codes.Code) -> str:
    return hashlib.sha256((parsed.gtin + '\0' + parsed.serial).encode('ascii')).hexdigest()


def archive_for_code(value: str) -> int | None:
    """Find the active binding of a full code, including its crypto fields.

    The existing unique fingerprint limits decryption to one candidate. It
    identifies a unit only: the full payload must still match, case included.
    """
    try:
        parsed = marking_codes.parse(value)
    except ValueError:
        return None
    row = db.query_one('SELECT c.code, m.archive_id FROM marking_codes c '
                       'JOIN marking_orders m ON m.id=c.marking_order_id '
                       "WHERE c.fingerprint=? AND (c.active=1 OR c.status='detached')", (_fingerprint(parsed),))
    if row and marking_codes.equivalent(_open(row['code']), parsed.raw):
        return row['archive_id']
    return None


def needs_validation(saved: dict | None) -> bool:
    return bool(saved and (has_required(saved) or exemplar_data.required(saved) or codes(saved)))


def guard(account: dict, order_id: str) -> None:
    """Общая защита завершения, в том числе прямых/старых маршрутов."""
    saved = order(account['id'], order_id)
    if needs_validation(saved) and saved['status'] != 'accepted':
        notice = _notice(codes(saved))
        raise ValueError(notice['message'] if notice and notice['status'] != 'OK' else saved.get('error') or
                         'Передайте обязательные коды «Честный знак» и данные товара и дождитесь принятия площадкой')


def local_only(account: dict, saved: dict | None) -> bool:
    """Local retention never means marketplace acceptance; completion is rechecked separately."""
    return bool(account['marketplace'] == 'yandex' and saved and saved['status'] == 'not_recorded'
                and json.loads(_open(saved['context'])).get('_local_completion')
                and exemplar_data.ready(saved))


def item_key(account: dict, item: dict) -> str:
    provider = source(account)
    return str(provider.item_key(item)) if provider else ''


def pending(account: dict, state: dict, *, required_only: bool = True) -> tuple[dict, int] | None:
    active = state.get('active') or {}
    saved = order(account['id'], str(active.get('posting_number') or active.get('id') or ''))
    if not saved:
        return None
    by_key = {p['key']: p for p in products(saved)}
    existing = {(r['item_key'], r['unit_no']): r for r in codes(saved)}
    for item in state.get('items', []):
        key = item_key(account, item)
        product = by_key.get(key)
        if not product or not product.get('required') and (required_only or not product.get('possible')):
            continue
        for unit in range(1, min(int(item.get('scanned') or 0), int(product['quantity'])) + 1):
            row = existing.get((key, unit))
            if row is None or row['status'] == 'error':
                return product, unit
    return None


def _notice(marks: list[dict]) -> dict | None:
    priority = {'INVALID': 0, 'FAILED': 1, 'IN_PROGRESS': 2, 'NOT_ON_VALIDATION': 3, 'OK': 4}
    checked = [m for m in marks if m.get('check_message') and m.get('check_status') in priority
               and (m['check_status'] != 'OK' or m['status'] == 'accepted')]
    if not checked:
        return None
    mark = min(checked, key=lambda m: priority[m['check_status']])
    return {'status': mark['check_status'], 'substatus': mark['check_substatus'], 'message': mark['check_message'],
            'kind': 'error' if mark['check_status'] == 'INVALID' else 'ok' if mark['check_status'] == 'OK' else 'warning'}


def enrich(account: dict, state: dict) -> dict:
    active = state.get('active') or {}
    key = str(active.get('posting_number') or active.get('id') or '')
    saved = order(account['id'], key) if key else None
    if not saved:
        return state
    by_key = {p['key']: p for p in products(saved)}
    marks = codes(saved)
    for item in state.get('items', []):
        product = by_key.get(item_key(account, item))
        if not product:
            continue
        item['exemplar_data'] = exemplar_data.public(saved, product['key'])
        related = [m for m in marks if m['item_key'] == product['key']]
        if not product.get('required') and not related:
            continue
        not_recorded = any(m['status'] == 'not_recorded' for m in related)
        failed = not_recorded or any(m['status'] == 'error' for m in related) or (
            saved['status'] == 'error' and not json.loads(saved.get('field_errors') or '{}') and
            not any(m['status'] == 'error' for m in marks))
        ready = len(related) >= int(product['quantity']) and all(m['status'] == 'accepted' for m in related)
        notice = _notice(related)
        item['marking'] = {
            'key': product['key'],
            'status': 'error' if failed else 'accepted' if ready else 'waiting',
            'check_status': notice['status'] if notice else None,
            'check_substatus': notice['substatus'] if notice else None,
            'title': NOT_RECORDED if not_recorded else notice['message'] if notice else 'Честный знак не прошёл проверку' if failed else 'Честный знак принят площадкой' if ready else
                     saved.get('error') or 'Честный знак не отсканирован или проверяется',
        }
    waiting = pending(account, state)
    local_completion = local_only(account, saved)
    blocked = needs_validation(saved) and saved['status'] != 'accepted' and not local_completion
    notice = _notice(marks)
    if notice and notice['status'] == 'OK' and saved['status'] != 'accepted':
        notice = None
    state['marking'] = {'pending_key': waiting[0]['key'] if waiting else None,
                        'pending_unit': waiting[1] if waiting else None,
                        'status': saved['status'], 'error': saved.get('error'), 'blocked': blocked,
                        'poll': saved['status'] in {'queued', 'checking', 'uncertain', 'not_recorded'},
                        'local_only': local_completion,
                        'notice': None if local_completion else notice,
                        'message': LOCAL_ONLY_MESSAGE if local_completion else ''}
    if blocked:
        state['complete'] = False
    return state


def _scan_owner(fingerprint, account, order_id):
    owner = marking_registry.binding(fingerprint)
    if owner and owner['state'] != 'released':
        marking_returns.process_order(owner['account_id'], owner['order_id'])
        owner = marking_registry.binding(fingerprint)
    if owner and owner['state'] != 'released' and not marking_registry.owns(owner, account['id'], order_id):
        if owner['marketplace'] == 'ozon':
            from ..markets.ozon.marking_returns import try_release
            try_release(owner)
            owner = marking_registry.binding(fingerprint)
        if owner['state'] != 'released':
            from . import marking_replacements
            owner_account = accounts.get(owner['account_id'])
            if owner_account and owner_account['active'] and accounts.is_configured(owner_account):
                marking_replacements.reconcile(owner_account, owner['order_id'])
                owner = marking_registry.binding(fingerprint)
        if owner['state'] != 'released':
            raise ValueError(marking_registry.conflict(owner))
    return owner


def _check_gtin(parsed: marking_codes.Code, item: dict) -> None:
    # Сопоставление проверяем, если каталог содержит числовой EAN/GTIN.
    known = {str(c).zfill(14) for c in item.get('barcodes') or [] if str(c).isdigit() and len(str(c)) in {12, 13, 14}}
    if known and parsed.gtin not in known:
        raise ValueError('GTIN маркировки не совпадает с отсканированным товаром')


def _resume_scan(saved: dict, user: dict) -> None:
    if saved['status'] == 'accepted':
        return
    db.execute('UPDATE marking_orders SET rescan_requested=1,checked_at=NULL,attempts=0,user_id=? WHERE id=?', (user['id'], saved['id']))
    exemplar_data.queue(order(saved['account_id'], saved['order_id']), user['id'])
    if _worker:
        _worker.wake.set()


def save_scan(account: dict, user: dict, state: dict, value: str) -> dict:
    parsed = marking_codes.parse(value)
    active = state.get('active') or {}
    order_id = str(active.get('posting_number') or active.get('id') or '')
    if not order_id:
        raise ValueError('Сначала откройте отправление и отсканируйте ШК товара')
    saved = order(account['id'], order_id)
    if not saved:
        raise ValueError('Для этого заказа не получены требования к маркировке')
    fingerprint = _fingerprint(parsed)
    owner = _scan_owner(fingerprint, account, order_id)
    same = db.query_one('SELECT * FROM marking_codes WHERE fingerprint = ?', (fingerprint,))
    if same and owner is None:
        owner = marking_registry.binding(fingerprint)
    replacing_same = False
    restoring = False
    recycling = bool(owner and owner['state'] == 'released')
    if same:
        if same['marking_order_id'] == saved['id'] and same['active'] and marking_codes.equivalent(_open(same['code']), parsed.raw) and same['status'] != 'error':
            _resume_scan(saved, user)
            return {'duplicate': True, 'id': same['id']}
        replacing_same = same['marking_order_id'] == saved['id'] and same['active'] and same['status'] == 'error'
        restoring = same['marking_order_id'] == saved['id'] and not same['active'] and same['status'] == 'removed'
        if not replacing_same and not restoring and not recycling:
            raise ValueError(marking_registry.conflict(owner))
    slot = pending(account, state, required_only=False)
    if not slot:
        raise ValueError('Сначала отсканируйте ШК маркируемого товара; для каждой единицы нужен отдельный скан')
    product, unit = slot
    if (replacing_same or restoring) and (same['item_key'], same['unit_no']) != (product['key'], unit):
        raise ValueError('Этот код «Честный знак» относится к другой единице товара')
    item = next(i for i in state['items'] if item_key(account, i) == product['key'])
    _check_gtin(parsed, item)
    mark_id = _store_scan(account, user, saved, parsed, fingerprint, product, unit, same if replacing_same or restoring or recycling else None)
    if _worker:
        _worker.wake.set()
    return {'duplicate': bool(replacing_same and marking_codes.equivalent(_open(same['code']), parsed.raw)), 'id': mark_id}


def _store_scan(account, user, saved, parsed, fingerprint, product, unit, same):
    order_id = saved['order_id']
    replacing_same = same is not None
    with order_lock.hold(account['id'], order_id):
        with db.write() as conn:
            current = conn.execute('SELECT account_id,posting_number FROM pack_state WHERE user_id=?',
                                   (user['id'],)).fetchone()
            if not current or (current['account_id'], current['posting_number']) != (account['id'], order_id):
                raise ValueError('Начатая сборка изменилась. Повторите скан в актуальном заказе.')
            pack_state.claim(conn, account, user, order_id)
            owner = marking_registry.binding(fingerprint, conn)
            if owner and owner['state'] != 'released' and not marking_registry.owns(owner, account['id'], order_id):
                raise ValueError(marking_registry.conflict(owner))
            old = conn.execute('SELECT * FROM marking_codes WHERE marking_order_id = ? AND item_key = ? '
                               'AND unit_no = ? AND active = 1', (saved['id'], product['key'], unit)).fetchone()
            if old:
                if old['status'] != 'error':
                    raise ValueError('У этой единицы уже есть код «Честный знак»')
                if not same or old['id'] != same['id']:
                    conn.execute('UPDATE marking_codes SET active = 0 WHERE id = ?', (old['id'],))
            if replacing_same:
                mark_id = same['id']
                conn.execute("UPDATE marking_codes SET marking_order_id=?,item_key=?,unit_no=?,code=?,active=1,status='saved',error=NULL,"
                             'check_status=NULL,check_substatus=NULL,check_message=NULL,'
                             'created_at=?,created_by=? WHERE id=?',
                             (saved['id'], product['key'], unit, _seal(parsed.raw), db.now_iso(), user['login'], mark_id))
            else:
                mark_id = conn.execute(
                    'INSERT INTO marking_codes(marking_order_id,item_key,unit_no,fingerprint,code,gtin,serial,created_at,created_by) '
                    'VALUES(?,?,?,?,?,?,?,?,?)', (saved['id'], product['key'], unit, fingerprint, _seal(parsed.raw),
                                                 parsed.gtin, parsed.serial, db.now_iso(), user['login'])).lastrowid
            marking_registry.claim(conn, fingerprint, account, saved, mark_id)
            conn.execute("UPDATE marking_orders SET revision=revision+1,status='collecting',error=NULL,rescan_requested=0,user_id=? "
                         'WHERE id = ?', (user['id'], saved['id']))
            db.log_event('marking_saved', account_id=account['id'], user=user, posting_number=order_id,
                         message=f'Честный знак сохранён: позиция {product["key"]}, единица {unit}', conn=conn)
        latest = order(account['id'], order_id)
        exemplar_data.queue(latest, user['id'])
    return mark_id


def _reject(saved: dict, marks: list[dict], errors: dict[str, str], message: str):
    with db.write() as conn:
        for mark in marks:
            error = errors.get(f'{mark["item_key"]}:{mark["unit_no"]}') or errors.get(mark['item_key'])
            if error:
                conn.execute("UPDATE marking_codes SET status='error',error=? WHERE id=?", (error, mark['id']))
        field_errors = {k: v for k, v in errors.items() if k.startswith('data:')}
        conn.execute("UPDATE marking_orders SET status='error',error=?,field_errors=?,checked_at=? WHERE id=?",
                     (message, json.dumps(field_errors), db.now_iso(), saved['id']))


def _not_recorded(account: dict, saved: dict, marks: list[dict], exc: NotRecorded):
    # Local retention is independent of remote acceptance: keep the encrypted code,
    # its archive and its global reservation. Subsequent jobs reconcile by reading.
    payload = {'marking_order_id': saved['id'], 'revision': saved['revision']}
    context = json.loads(_open(saved['context']))
    context['_local_completion'] = exc.local_completion
    with db.write() as conn:
        for mark in marks:
            if f'{mark["item_key"]}:{mark["unit_no"]}' in exc.errors:
                conn.execute("UPDATE marking_codes SET status='not_recorded',error=? WHERE id=?",
                             (NOT_RECORDED, mark['id']))
        conn.execute("UPDATE marking_orders SET status='not_recorded',error=?,context=?,checked_at=?,attempts=attempts+1 WHERE id=?",
                     (NOT_RECORDED, _seal(json.dumps(context, ensure_ascii=False)), db.now_iso(), saved['id']))
        recorded = conn.execute("SELECT 1 FROM events WHERE kind='marking_not_recorded' AND account_id=? "
                                'AND posting_number=? AND payload=? LIMIT 1',
                                (account['id'], saved['order_id'], json.dumps(payload, ensure_ascii=False))).fetchone()
        if not recorded:
            actor = conn.execute('SELECT id,login FROM users WHERE id=?', (saved['user_id'],)).fetchone()
            db.log_event('marking_not_recorded', level='error', account_id=account['id'],
                         user=dict(actor) if actor else None, posting_number=saved['order_id'],
                         message=NOT_RECORDED, payload=payload, conn=conn)


def _record_checks(saved, marks, checks):
    if checks is None:
        return
    with db.write() as conn:
        for mark in marks:
            check = checks.get(f'{mark["item_key"]}:{mark["unit_no"]}', {})
            conn.execute('UPDATE marking_codes SET check_status=?,check_substatus=?,check_message=? '
                         'WHERE id=? AND marking_order_id=? AND active=1',
                         (check.get('status'), check.get('substatus'), check.get('message'), mark['id'], saved['id']))


def _rejection(account, saved, marks, exc):
    _record_checks(saved, marks, exc.checks)
    if isinstance(exc, NotRecorded):
        _not_recorded(account, saved, marks, exc)
    else:
        _reject(saved, marks, exc.errors,
                'Площадка отклонила данные. Замените отмеченную марку или исправьте обязательные данные заказа.')


def _finalize(account, saved, provider, context, marks):
    try:
        outcome = provider.finalize(account, saved['order_id'], context, marks)
    except (AwaitingCheck, Rejected) as exc:
        _record_checks(saved, marks, exc.checks)
        raise
    if outcome:
        _record_checks(saved, marks, outcome.get('checks'))


def confirm(account: dict, saved: dict, context: dict, marks: list[dict], *, poll: bool = True) -> None:
    """Completion must persist a failed recheck instead of retaining a stale green M."""
    provider = source(account)
    try:
        if poll:
            outcome = provider.poll(account, saved['order_id'], context, marks)
            _record_checks(saved, marks, outcome.get('checks'))
            if outcome['status'] == 'error':
                raise Rejected(outcome.get('errors') or {}, checks=outcome.get('checks'))
            if outcome['status'] != 'accepted':
                raise AwaitingCheck(outcome)
        _finalize(account, saved, provider, context, marks)
    except Rejected as exc:
        _rejection(account, saved, marks, exc)
        raise
    except Exception as exc:
        with db.write() as conn:
            conn.execute("UPDATE marking_orders SET status='uncertain',error=?,checked_at=? WHERE id=?",
                         (str(exc) if isinstance(exc, AwaitingCheck) else 'Площадка пока не подтвердила актуальные данные экземпляров. Завершение заблокировано.',
                          db.now_iso(), saved['id']))
            conn.execute("UPDATE marking_codes SET status='saved',error=NULL WHERE marking_order_id=? "
                         "AND active=1 AND status='accepted'", (saved['id'],))
        raise


def _accept(account, saved, provider, context, marks):
    from . import readiness

    order_id = saved['order_id']
    # Обновление существующего отправления, переход нового и повторное
    # доказательство применения полных кодов после перехода статуса.
    _finalize(account, saved, provider, context, marks)
    user_row = db.query_one('SELECT id,login,role FROM users WHERE id=?', (saved['user_id'],))
    if not user_row:
        raise ValueError('Не найден сотрудник, отправивший маркировку')
    readiness.call(account, dict(user_row), order_id)
    if provider.redistribute and provider.redistribute(account, dict(user_row), order_id, marks):
        return
    _finalize(account, saved, provider, context, marks)
    prepare(account, order_id)
    with db.write() as conn:
        conn.execute("UPDATE marking_orders SET status='accepted',error=NULL,field_errors='{}',checked_at=? WHERE id=?",
                     (db.now_iso(), saved['id']))
        conn.execute("UPDATE marking_codes SET status='accepted',error=NULL WHERE marking_order_id=? AND active=1", (saved['id'],))
        db.log_event('marking_accepted', account_id=account['id'], user=dict(user_row), posting_number=order_id,
                     message=f'Площадка приняла {len(marks)} кодов «Честный знак»; готовность подтверждена', conn=conn)
    from . import marking_replacements
    marking_replacements.reconcile(account, order_id)


def _submit(account, saved, provider, context, marks):
    def dispatch():
        # Чтение/валидация могут упасть до записи. Только сама запись
        # переводит задачу в «могло уйти», переживая SIGKILL.
        db.execute("UPDATE marking_orders SET status='uncertain',submitted_revision=revision,checked_at=? WHERE id=?",
                   (db.now_iso(), saved['id']))
    provider.submit(account, saved['order_id'], context, marks, dispatch)
    db.execute("UPDATE marking_orders SET status='checking',error=NULL,attempts=0,rescan_requested=0,checked_at=? WHERE id=?",
               (db.now_iso(), saved['id']))


def _check(account, saved, provider, context, marks):
    outcome = provider.poll(account, saved['order_id'], context, marks)
    _record_checks(saved, marks, outcome.get('checks'))
    if outcome['status'] == 'error':
        _reject(saved, marks, outcome.get('errors') or {},
                'Площадка отклонила данные. Исправьте отмеченное поле или повторно отсканируйте код «Честный знак».')
    elif outcome['status'] == 'accepted':
        _accept(account, saved, provider, context, marks)
    elif outcome['status'] == 'retry' and saved.get('rescan_requested'):
        # A fresh explicit scan must not inherit minutes of backoff from an
        # abandoned attempt. Only the safe provider retry proof reaches here.
        db.execute("UPDATE marking_orders SET status='queued',checked_at=NULL,attempts=0,error=? WHERE id=?",
                   (outcome.get('message'), saved['id']))
        if _worker:
            _worker.wake.set()
    else:
        # Повтор записи допустим лишь после явного ответа, что проверка не
        # выполнена. Таймаут сам по себе не основание отправить код повторно.
        db.execute('UPDATE marking_orders SET status=?,checked_at=?,error=?,attempts=attempts+1 WHERE id=?',
                   ('queued' if outcome['status'] == 'retry' else saved['status'],
                    db.now_iso(), outcome.get('message'), saved['id']))


def _connection_failure(account, saved, exc):
    # Не сохраняем внешний текст: он может содержать коды и ключи.
    log.warning('Передача кодов «Честный знак»: кабинет %s, заказ %s, %s', account['id'], saved['order_id'], type(exc).__name__)
    if isinstance(exc, AwaitingCheck):
        _record_checks(saved, codes(saved), exc.checks)
        db.execute("UPDATE marking_orders SET status='checking',error=?,checked_at=?,attempts=attempts+1 WHERE id=?",
                   (str(exc), db.now_iso(), saved['id']))
        db.execute("UPDATE marking_codes SET status='saved',error=NULL WHERE marking_order_id=? AND active=1 AND status='accepted'", (saved['id'],))
        return
    status = getattr(exc, 'status', None)
    # Отказ опроса не доказывает отказ предыдущей записи: после 401/429
    # на status нельзя автоматически повторно отправлять уже переданные ЧЗ.
    writing = saved['status'] == 'queued'
    definite = writing and status in {400, 404, 409, 422}
    if definite:
        _reject(saved, codes(saved), {m['item_key']: 'Площадка отклонила запись данных экземпляров' for m in codes(saved)},
                'Площадка отклонила данные экземпляров. Проверьте заказ и повторите скан кода «Честный знак».')
    next_status = 'error' if definite else 'queued' if writing and status in {401, 403, 429} else None
    db.execute('UPDATE marking_orders SET status=COALESCE(?,status),checked_at=?,error=?,attempts=attempts+1 WHERE id=?',
               (next_status, db.now_iso(), 'Площадка отклонила данные экземпляров.' if definite else
                'Пока не удалось подтвердить передачу кодов «Честный знак». Проверка будет повторена.', saved['id']))


def _actionable(saved: dict | None, statuses: set[str]) -> bool:
    if not saved or saved['status'] not in statuses:
        return False
    if needs_validation(saved):
        return True
    # Stop obsolete field-only jobs without claiming marketplace acceptance.
    db.execute("UPDATE marking_orders SET status='collecting',error=NULL,field_errors='{}' WHERE id=?",
               (saved['id'],))
    return False


def process(account: dict, order_id: str, *, force: bool = False) -> None:
    """Одна запись/проверка. Неопределённый результат записи — только опрос."""
    marking_returns.process_order(account['id'], order_id)
    statuses = {'queued', 'checking', 'uncertain', 'not_recorded'}
    saved = order(account['id'], order_id)
    if not saved or saved['status'] not in statuses:
        return
    delay = min(300, 5 * 2 ** min(int(saved['attempts']), 6))
    if not force and saved['checked_at'] and datetime.fromisoformat(saved['checked_at']) > datetime.now(timezone.utc) - timedelta(seconds=delay):
        return
    with order_lock.hold(account['id'], order_id):
        saved = order(account['id'], order_id)
        provider = source(account)
        if not provider or not _actionable(saved, statuses):
            return
        marks = []
        try:
            if saved['status'] == 'queued':
                # Reconcile before a new write so an externally removed,
                # previously accepted code is never silently submitted again.
                prepare(account, order_id)
                saved = order(account['id'], order_id)
                if saved['status'] != 'queued':
                    return
            context = exemplar_data.context(saved)
            marks = codes(saved, full=True)
            user_row = db.query_one('SELECT id,login,role FROM users WHERE id=?', (saved['user_id'],))
            if provider.redistribute and user_row and provider.redistribute(account, dict(user_row), order_id, marks):
                return
            if saved['status'] == 'queued':
                _submit(account, saved, provider, context, marks)
            else:
                _check(account, saved, provider, context, marks)
        except Rejected as exc:
            _rejection(account, saved, marks, exc)
        except Exception as exc:
            _connection_failure(account, saved, exc)


def run_once() -> None:
    marking_returns.process_pending()
    for row in db.query("SELECT account_id,order_id FROM marking_orders m WHERE status IN "
                        "('queued','checking','uncertain','not_recorded') AND (status!='not_recorded' OR EXISTS "
                        '(SELECT 1 FROM pack_state p WHERE p.account_id=m.account_id AND p.posting_number=m.order_id)) '
                        "ORDER BY CASE WHEN status='not_recorded' THEN 1 ELSE 0 END,COALESCE(checked_at,'') LIMIT 20"):
        account = accounts.get(row['account_id'])
        if account and account['active'] and accounts.is_configured(account):
            try:
                process(account, row['order_id'])
            except ValueError:  # другой процесс/запрос уже держит аренду
                continue
    from . import marking_replacements
    marking_replacements.run_once()


class Worker(threading.Thread):
    daemon = True

    def __init__(self):
        super().__init__(name='marking')
        self.stopping = threading.Event()
        self.wake = threading.Event()

    def run(self):  # pragma: no cover - run_once проверяется отдельно
        try:
            while not self.stopping.is_set():
                self.wake.clear()
                try:
                    run_once()
                except Exception:
                    log.exception('Не удалось обработать очередь кодов «Честный знак»')
                self.wake.wait(5)
        finally:
            conn = getattr(db._local, 'conn', None)
            if conn:
                conn.close()
                db._local.conn = None


_worker: Worker | None = None


def start_worker():
    global _worker
    if settings.sync_enabled and (_worker is None or not _worker.is_alive()):
        _worker = Worker()
        _worker.start()


def stop_worker():
    if _worker:
        _worker.stopping.set()
        _worker.wake.set()


def retained(archive_id: int) -> dict | None:
    from . import sticker_archive

    row = db.query_one('SELECT o.* FROM sticker_orders o WHERE o.id=? AND (o.created_at>=? OR EXISTS '
                       '(SELECT 1 FROM sticker_versions v WHERE v.archive_id=o.id AND v.saved_at>=?) OR EXISTS '
                       '(SELECT 1 FROM cargo_labels c WHERE c.account_id=o.account_id AND c.order_id=o.order_id AND c.saved_at>=?))',
                       (archive_id, sticker_archive.cutoff(), sticker_archive.cutoff(), sticker_archive.cutoff()))
    return dict(row) if row else None


def archive_details(archive_id: int) -> dict:
    archived = retained(archive_id)
    if not archived:
        raise LookupError('Стикер не найден или срок хранения истёк')
    saved = order(archived['account_id'], archived['order_id'])
    rows = []
    if saved:
        by_key = {p['key']: p for p in products(saved)}
        for mark in codes(saved) + marking_registry.history(archive_id):
            product = by_key.get(mark['item_key']) or {}
            name = db.query_one('SELECT name FROM products WHERE account_id=? AND sku=?',
                                (archived['account_id'], product.get('sku', '')))
            historical = 'released_at' in mark
            owner = None if historical else marking_registry.binding(mark['fingerprint'])
            rows.append({k: mark.get(k) for k in ('id', 'item_key', 'unit_no', 'gtin', 'serial', 'status', 'error', 'created_at', 'created_by',
                                                'check_status', 'check_substatus', 'check_message')}
                        | {'name': (name['name'] if name else None) or product.get('sku') or mark['item_key'],
                           'history': historical, 'status': 'detached' if historical else mark['status'],
                           'printable': mark['status'] != 'error',
                           'can_unbind': not historical and archived['marketplace'] == 'ozon' and saved['status'] not in marking_registry.BUSY,
                           'unbind_version': str(owner['revision']) if owner else None})
    return {'order_id': archived['order_id'], 'marketplace': archived['marketplace'], 'items': rows,
            'status': saved['status'] if saved else 'collecting', 'error': saved.get('error') if saved else None}


def print_pdf(ids: list[int], *, history_ids=()) -> bytes:
    from . import printers

    if not (ids or history_ids) or len(ids) + len(history_ids) > 100:
        raise ValueError('Выберите от 1 до 100 кодов «Честный знак»')
    selected = []
    entries = [(mark_id, False) for mark_id in dict.fromkeys(ids)] + [(mark_id, True) for mark_id in dict.fromkeys(history_ids)]
    for mark_id, historical in entries:
        row = (db.query_one('SELECT * FROM marking_releases WHERE id=?', (mark_id,)) if historical else
               db.query_one('SELECT c.*, o.archive_id FROM marking_codes c JOIN marking_orders o ON o.id=c.marking_order_id '
                            'WHERE c.id=? AND c.active=1', (mark_id,)))
        if not row or not retained(row['archive_id']):
            raise LookupError('Код «Честный знак» не найден или срок хранения истёк')
        if row['status'] == 'error':
            raise ValueError('Этот код «Честный знак» отклонён площадкой. Замените марку перед повторной печатью.')
        selected.append(marking_codes.parse(_open(row['code'])))
    paper = next(r for r in printers.rows() if r['kind'] == 'marking:duplicate')
    return marking_codes.pdf(selected, *printers.paper_mm(paper))
