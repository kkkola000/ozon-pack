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

from . import accounts, crypto, db, exemplar_data, marking_codes, order_lock, pack_state
from .config import settings

log = logging.getLogger(__name__)
KV_AUTO = 'print_marking_duplicates'


class Rejected(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__('Площадка отклонила данные экземпляров')
        self.errors = errors
SCHEMA = order_lock.SCHEMA + """
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
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_marking_unit ON marking_codes(marking_order_id,item_key,unit_no)
    WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_marking_pending ON marking_orders(status,checked_at);
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
        raise ValueError('Не удалось расшифровать сохранённый ЧЗ. Проверьте ключ шифрования панели.') from exc


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
            raise ValueError('Не удалось подтвердить требования к уже сохранённым ЧЗ заказа')
        return None
    products = data['products']
    if not products or sum(int(p['quantity']) for p in products) > 500:
        raise ValueError('Не удалось безопасно получить состав экземпляров заказа')
    if any(not p.get('key') or not 1 <= int(p['quantity']) <= 500 for p in products):
        raise ValueError('Площадка вернула некорректный состав экземпляров')
    if saved:
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
                   ('Состав или требования заказа изменились. Сверьте уже сохранённые ЧЗ перед продолжением.', saved['id']))
        raise ValueError('Состав заказа изменился; сохранённые ЧЗ требуют сверки')
    if saved['status'] in {'queued', 'checking', 'uncertain'}:
        raise ValueError('Требования изменились во время передачи. Дождитесь проверки данных площадкой.')
    for product in products:
        old = old_policy.get(product['key'], {})
        product['required'] = bool(product.get('required') or old.get('required'))
        product['fields'] = sorted(set(product.get('fields', [])) | set(old.get('fields', [])))
    db.execute("UPDATE marking_orders SET products=?,context=?,status='collecting',revision=revision+1 WHERE id=?",
               (json.dumps(products), _seal(json.dumps(data['context'], ensure_ascii=False)), saved['id']))
    return order(saved['account_id'], saved['order_id'])


def products(saved: dict) -> list[dict]:
    return json.loads(saved['products'])


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


def needs_validation(saved: dict | None) -> bool:
    return bool(saved and (has_required(saved) or exemplar_data.required(saved) or codes(saved)))


def guard(account: dict, order_id: str) -> None:
    """Общая защита завершения, в том числе прямых/старых маршрутов."""
    saved = order(account['id'], order_id)
    if needs_validation(saved) and saved['status'] != 'accepted':
        raise ValueError(saved.get('error') or 'Передайте обязательные ЧЗ и данные товара и дождитесь принятия площадкой')


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
        failed = any(m['status'] == 'error' for m in related) or (
            saved['status'] == 'error' and not json.loads(saved.get('field_errors') or '{}') and
            not any(m['status'] == 'error' for m in marks))
        ready = len(related) >= int(product['quantity']) and all(m['status'] == 'accepted' for m in related)
        item['marking'] = {
            'key': product['key'],
            'status': 'error' if failed else 'accepted' if ready else 'waiting',
            'title': 'ЧЗ не прошёл проверку' if failed else 'ЧЗ принят площадкой' if ready else
                     saved.get('error') or 'ЧЗ не отсканирован или проверяется',
        }
    waiting = pending(account, state)
    blocked = needs_validation(saved) and saved['status'] != 'accepted'
    state['marking'] = {'pending_key': waiting[0]['key'] if waiting else None,
                        'pending_unit': waiting[1] if waiting else None,
                        'status': saved['status'], 'error': saved.get('error'), 'blocked': blocked,
                        'poll': saved['status'] in {'queued', 'checking', 'uncertain'}}
    if blocked:
        state['complete'] = False
    return state


def save_scan(account: dict, user: dict, state: dict, value: str) -> dict:
    parsed = marking_codes.parse(value)
    active = state.get('active') or {}
    order_id = str(active.get('posting_number') or active.get('id') or '')
    if not order_id:
        raise ValueError('Сначала откройте отправление и отсканируйте ШК товара')
    saved = order(account['id'], order_id)
    if not saved:
        raise ValueError('Для этого заказа не получены требования к маркировке')
    fingerprint = hashlib.sha256((parsed.gtin + '\0' + parsed.serial).encode('ascii')).hexdigest()
    same = db.query_one('SELECT * FROM marking_codes WHERE fingerprint = ?', (fingerprint,))
    replacing_same = False
    if same:
        if same['marking_order_id'] == saved['id'] and same['active'] and marking_codes.equivalent(_open(same['code']), parsed.raw) and same['status'] != 'error':
            return {'duplicate': True, 'id': same['id']}
        replacing_same = same['marking_order_id'] == saved['id'] and same['active'] and same['status'] == 'error'
        if not replacing_same:
            raise ValueError('Этот экземпляр ЧЗ уже связан с заказом. Один код нельзя использовать повторно.')
    slot = pending(account, state, required_only=False)
    if not slot:
        raise ValueError('Сначала отсканируйте ШК маркируемого товара; для каждой единицы нужен отдельный скан')
    product, unit = slot
    if replacing_same and (same['item_key'], same['unit_no']) != (product['key'], unit):
        raise ValueError('Этот ЧЗ относится к другой единице товара')
    item = next(i for i in state['items'] if item_key(account, i) == product['key'])
    # Сопоставление проверяем, если каталог содержит числовой EAN/GTIN.
    known = {str(c).zfill(14) for c in item.get('barcodes') or [] if str(c).isdigit() and len(str(c)) in {12, 13, 14}}
    if known and parsed.gtin not in known:
        raise ValueError('GTIN маркировки не совпадает с отсканированным товаром')
    mark_id = _store_scan(account, user, saved, parsed, fingerprint, product, unit, same if replacing_same else None)
    if _worker:
        _worker.wake.set()
    return {'duplicate': bool(replacing_same and _open(same['code']) == parsed.raw), 'id': mark_id}


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
            old = conn.execute('SELECT * FROM marking_codes WHERE marking_order_id = ? AND item_key = ? '
                               'AND unit_no = ? AND active = 1', (saved['id'], product['key'], unit)).fetchone()
            if old:
                if old['status'] != 'error':
                    raise ValueError('У этой единицы уже есть ЧЗ')
                if not replacing_same:
                    conn.execute('UPDATE marking_codes SET active = 0 WHERE id = ?', (old['id'],))
            if replacing_same:
                mark_id = same['id']
                conn.execute("UPDATE marking_codes SET code=?,status='saved',error=NULL WHERE id=?",
                             (_seal(parsed.raw), mark_id))
            else:
                mark_id = conn.execute(
                    'INSERT INTO marking_codes(marking_order_id,item_key,unit_no,fingerprint,code,gtin,serial,created_at,created_by) '
                    'VALUES(?,?,?,?,?,?,?,?,?)', (saved['id'], product['key'], unit, fingerprint, _seal(parsed.raw),
                                                 parsed.gtin, parsed.serial, db.now_iso(), user['login'])).lastrowid
            conn.execute("UPDATE marking_orders SET revision=revision+1,status='collecting',error=NULL,user_id=? "
                         'WHERE id = ?', (user['id'], saved['id']))
            db.log_event('marking_saved', account_id=account['id'], user=user, posting_number=order_id,
                         message=f'ЧЗ сохранён: позиция {product["key"]}, единица {unit}', conn=conn)
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


def _accept(account, saved, provider, context, marks):
    from . import readiness

    order_id = saved['order_id']
    # Обновление существующего отправления, переход нового и повторное
    # доказательство применения полных кодов после перехода статуса.
    provider.finalize(account, order_id, context, marks)
    user_row = db.query_one('SELECT id,login,role FROM users WHERE id=?', (saved['user_id'],))
    if not user_row:
        raise ValueError('Не найден сотрудник, отправивший маркировку')
    readiness.call(account, dict(user_row), order_id)
    if provider.redistribute and provider.redistribute(account, dict(user_row), order_id, marks):
        return
    provider.finalize(account, order_id, context, marks)
    prepare(account, order_id)
    with db.write() as conn:
        conn.execute("UPDATE marking_orders SET status='accepted',error=NULL,field_errors='{}',checked_at=? WHERE id=?",
                     (db.now_iso(), saved['id']))
        conn.execute("UPDATE marking_codes SET status='accepted',error=NULL WHERE marking_order_id=? AND active=1", (saved['id'],))
        db.log_event('marking_accepted', account_id=account['id'], user=dict(user_row), posting_number=order_id,
                     message=f'Площадка приняла {len(marks)} кодов ЧЗ; готовность подтверждена', conn=conn)


def _submit(account, saved, provider, context, marks):
    def dispatch():
        # Чтение/валидация могут упасть до записи. Только сама запись
        # переводит задачу в «могло уйти», переживая SIGKILL.
        db.execute("UPDATE marking_orders SET status='uncertain',submitted_revision=revision,checked_at=? WHERE id=?",
                   (db.now_iso(), saved['id']))
    provider.submit(account, saved['order_id'], context, marks, dispatch)
    db.execute("UPDATE marking_orders SET status='checking',error=NULL,attempts=0,checked_at=? WHERE id=?",
               (db.now_iso(), saved['id']))


def _check(account, saved, provider, context, marks):
    outcome = provider.poll(account, saved['order_id'], context, marks)
    if outcome['status'] == 'error':
        _reject(saved, marks, outcome.get('errors') or {},
                'Площадка отклонила данные. Исправьте отмеченное поле или повторно отсканируйте ЧЗ.')
    elif outcome['status'] == 'accepted':
        _accept(account, saved, provider, context, marks)
    else:
        # Повтор записи допустим лишь после явного ответа, что проверка не
        # выполнена. Таймаут сам по себе не основание отправить код повторно.
        db.execute('UPDATE marking_orders SET status=?,checked_at=?,error=?,attempts=attempts+1 WHERE id=?',
                   ('queued' if outcome['status'] == 'retry' else saved['status'],
                    db.now_iso(), outcome.get('message'), saved['id']))


def _connection_failure(account, saved, exc):
    # Не сохраняем внешний текст: он может содержать коды и ключи.
    log.warning('Передача ЧЗ: кабинет %s, заказ %s, %s', account['id'], saved['order_id'], type(exc).__name__)
    status = getattr(exc, 'status', None)
    # Отказ опроса не доказывает отказ предыдущей записи: после 401/429
    # на status нельзя автоматически повторно отправлять уже переданные ЧЗ.
    writing = saved['status'] == 'queued'
    definite = writing and status in {400, 404, 409, 422}
    if definite:
        _reject(saved, codes(saved), {m['item_key']: 'Площадка отклонила запись данных экземпляров' for m in codes(saved)},
                'Площадка отклонила данные экземпляров. Проверьте заказ и повторите скан ЧЗ.')
    next_status = 'error' if definite else 'queued' if writing and status in {401, 403, 429} else None
    db.execute('UPDATE marking_orders SET status=COALESCE(?,status),checked_at=?,error=?,attempts=attempts+1 WHERE id=?',
               (next_status, db.now_iso(), 'Площадка отклонила данные экземпляров.' if definite else
                'Пока не удалось подтвердить передачу ЧЗ. Проверка будет повторена.', saved['id']))


def process(account: dict, order_id: str, *, force: bool = False) -> None:
    """Одна запись/проверка. Неопределённый результат записи — только опрос."""
    statuses = {'queued', 'checking', 'uncertain'}
    saved = order(account['id'], order_id)
    if not saved or saved['status'] not in statuses:
        return
    delay = min(300, 5 * 2 ** min(int(saved['attempts']), 6))
    if not force and saved['checked_at'] and datetime.fromisoformat(saved['checked_at']) > datetime.now(timezone.utc) - timedelta(seconds=delay):
        return
    with order_lock.hold(account['id'], order_id):
        saved = order(account['id'], order_id)
        provider = source(account)
        if not provider or not saved or saved['status'] not in statuses:
            return
        marks = []
        try:
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
            _reject(saved, marks, exc.errors,
                    'Площадка отклонила данные. Замените отмеченную марку или исправьте обязательные данные заказа.')
        except Exception as exc:
            _connection_failure(account, saved, exc)


def run_once() -> None:
    for row in db.query("SELECT account_id,order_id FROM marking_orders WHERE status IN "
                        "('queued','checking','uncertain') ORDER BY COALESCE(checked_at,'') LIMIT 20"):
        account = accounts.get(row['account_id'])
        if account and account['active'] and accounts.is_configured(account):
            try:
                process(account, row['order_id'])
            except ValueError:  # другой процесс/запрос уже держит аренду
                continue


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
                    log.exception('Не удалось обработать очередь ЧЗ')
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
        for mark in codes(saved):
            product = by_key.get(mark['item_key']) or {}
            name = db.query_one('SELECT name FROM products WHERE account_id=? AND sku=?',
                                (archived['account_id'], product.get('sku', '')))
            rows.append({k: mark[k] for k in ('id', 'item_key', 'unit_no', 'gtin', 'serial', 'status', 'error', 'created_at', 'created_by')}
                        | {'name': (name['name'] if name else None) or product.get('sku') or mark['item_key']})
    return {'order_id': archived['order_id'], 'marketplace': archived['marketplace'], 'items': rows,
            'status': saved['status'] if saved else 'collecting', 'error': saved.get('error') if saved else None}


def print_pdf(ids: list[int]) -> bytes:
    from . import printers

    if not ids or len(ids) > 100:
        raise ValueError('Выберите от 1 до 100 кодов ЧЗ')
    selected = []
    for mark_id in dict.fromkeys(ids):
        row = db.query_one('SELECT c.*, o.archive_id FROM marking_codes c JOIN marking_orders o ON o.id=c.marking_order_id '
                           'WHERE c.id=? AND c.active=1', (mark_id,))
        if not row or not retained(row['archive_id']):
            raise LookupError('Код ЧЗ не найден или срок хранения истёк')
        if row['status'] == 'error':
            raise ValueError('Этот ЧЗ отклонён площадкой. Замените марку перед повторной печатью.')
        selected.append(marking_codes.parse(_open(row['code'])))
    paper = next(r for r in printers.rows() if r['kind'] == 'marking:duplicate')
    return marking_codes.pdf(selected, *printers.paper_mm(paper))
