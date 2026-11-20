"""Durable ownership of a marking unit; released bindings retain archive history."""
from __future__ import annotations

from . import db, order_lock

BUSY = {'queued', 'checking', 'uncertain'}
SCHEMA = """
CREATE TABLE IF NOT EXISTS marking_registry (
 fingerprint TEXT PRIMARY KEY,
 account_id INTEGER NOT NULL,
 marketplace TEXT NOT NULL,
 order_id TEXT NOT NULL,
 code_id INTEGER REFERENCES marking_codes(id) ON DELETE SET NULL,
 state TEXT NOT NULL DEFAULT 'bound',
 revision INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS marking_releases (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 archive_id INTEGER NOT NULL REFERENCES sticker_orders(id) ON DELETE CASCADE,
 account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 fingerprint TEXT NOT NULL,
 item_key TEXT NOT NULL,
 unit_no INTEGER NOT NULL,
 code TEXT NOT NULL,
 gtin TEXT NOT NULL,
 serial TEXT NOT NULL,
 status TEXT NOT NULL,
 created_at TEXT NOT NULL,
 created_by TEXT NOT NULL,
 released_at TEXT NOT NULL,
 released_by TEXT NOT NULL,
 reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_marking_release_archive ON marking_releases(archive_id,fingerprint,id);
CREATE INDEX IF NOT EXISTS idx_marking_release_gtin ON marking_releases(gtin);
"""


def backfill(conn, *, account_id=None, order_id=None):
    # INSERT OR IGNORE preserves explicit releases across every restart.
    where, params = (' WHERE m.account_id=? AND m.order_id=?', (account_id, order_id)) if account_id is not None else ('', ())
    conn.execute('INSERT OR IGNORE INTO marking_registry(fingerprint,account_id,marketplace,order_id,code_id,updated_at) '
                 'SELECT c.fingerprint,m.account_id,a.marketplace,m.order_id,c.id,c.created_at '
                 'FROM marking_codes c JOIN marking_orders m ON m.id=c.marking_order_id '
                 'JOIN accounts a ON a.id=m.account_id' + where, params)


def binding(fingerprint: str, conn=None):
    conn = conn or db.connect()
    row = conn.execute('SELECT * FROM marking_registry WHERE fingerprint=?', (fingerprint,)).fetchone()
    if not row:
        # Also supports bindings created by older processes during an upgrade.
        conn.execute('INSERT OR IGNORE INTO marking_registry(fingerprint,account_id,marketplace,order_id,code_id,updated_at) '
                     'SELECT c.fingerprint,m.account_id,a.marketplace,m.order_id,c.id,c.created_at '
                     'FROM marking_codes c JOIN marking_orders m ON m.id=c.marking_order_id '
                     'JOIN accounts a ON a.id=m.account_id WHERE c.fingerprint=?', (fingerprint,))
        row = conn.execute('SELECT * FROM marking_registry WHERE fingerprint=?', (fingerprint,)).fetchone()
    return dict(row) if row else None


def conflict(owner: dict) -> str:
    return f'Данный Честный знак уже используется в заказе: {owner["order_id"]}'


def owns(owner, account_id, order_id):
    return owner and (owner['account_id'], owner['order_id']) == (account_id, order_id)


def claim(conn, fingerprint, account, saved, code_id):
    owner = binding(fingerprint, conn)
    if owner and owner['state'] != 'released' and not owns(owner, account['id'], saved['order_id']):
        raise ValueError(conflict(owner))
    conn.execute('INSERT INTO marking_registry(fingerprint,account_id,marketplace,order_id,code_id,state,updated_at) '
                 "VALUES(?,?,?,?,?,'bound',?) ON CONFLICT(fingerprint) DO UPDATE SET "
                 "account_id=excluded.account_id,marketplace=excluded.marketplace,order_id=excluded.order_id,"
                 "code_id=excluded.code_id,state='bound',revision=marking_registry.revision+1,updated_at=excluded.updated_at",
                 (fingerprint, account['id'], account['marketplace'], saved['order_id'], code_id, db.now_iso()))


def release(archive_id: int, code_id: int, *, user: dict | None = None, reason='manual', expected_version=None) -> bool:
    from . import marking

    archived = marking.retained(archive_id)
    if not archived or archived['marketplace'] != 'ozon':
        raise LookupError('Отвязка доступна для сохранённых отправлений Ozon')
    with order_lock.hold(archived['account_id'], archived['order_id']), db.write() as conn:
        row = conn.execute('SELECT c.*,m.archive_id,m.account_id,m.order_id,m.status AS order_status '
                           'FROM marking_codes c JOIN marking_orders m ON m.id=c.marking_order_id '
                           'WHERE c.id=? AND m.archive_id=?', (code_id, archive_id)).fetchone()
        if not row:
            raise LookupError('Код уже относится к другому отправлению или недоступен')
        owner = binding(row['fingerprint'], conn)
        return _release_record(conn, row, owner, user=user, reason=reason, expected_version=expected_version)


def _release_record(conn, row, owner, *, user, reason, expected_version=None, from_act=False):
    if not owns(owner, row['account_id'], row['order_id']):
        raise ValueError(conflict(owner))
    if owner['state'] == 'released':
        return False
    if expected_version is not None and str(owner['revision']) != expected_version:
        raise ValueError('Привязка Честного знака изменилась. Откройте список кодов заново.')
    if row['order_status'] in BUSY and reason != 'ozon_cancelled_before_assembly' and not from_act:
        raise ValueError('Дождитесь завершения передачи и проверки Честного знака площадкой перед отвязкой')
    if not row['active'] and not from_act:
        raise ValueError('Код уже снят с активной привязки')
    now = db.now_iso()
    conn.execute('INSERT INTO marking_releases(archive_id,account_id,fingerprint,item_key,unit_no,code,gtin,serial,status,'
                 'created_at,created_by,released_at,released_by,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (row['archive_id'], row['account_id'], row['fingerprint'], row['item_key'], row['unit_no'], row['code'],
                  row['gtin'], row['serial'], row['status'], row['created_at'], row['created_by'], now,
                  user['login'] if user else 'Ozon', reason))
    conn.execute("UPDATE marking_codes SET active=0,status='detached',error=NULL WHERE id=?", (row['id'],))
    conn.execute("UPDATE marking_registry SET state='released',revision=revision+1,updated_at=? WHERE fingerprint=?", (now, row['fingerprint']))
    conn.execute("UPDATE marking_orders SET status='collecting',revision=revision+1,submitted_revision=NULL,error=NULL,checked_at=NULL WHERE id=?",
                 (row['marking_order_id'],))
    db.log_event('marking_unbound', account_id=row['account_id'], user=user, posting_number=row['order_id'],
                 message=f'Честный знак отвязан: позиция {row["item_key"]}, единица {row["unit_no"]}; {reason}', conn=conn)
    return True


def release_for_act(conn, owner: dict, act_id: str, user: dict) -> bool:
    """Called inside the return job's order lease and SQLite transaction."""
    row = conn.execute('SELECT c.*,m.archive_id,m.account_id,m.order_id,m.status AS order_status '
                       'FROM marking_codes c JOIN marking_orders m ON m.id=c.marking_order_id WHERE c.id=?',
                       (owner['code_id'],)).fetchone()
    if row:
        return _release_record(conn, row, owner, user=user, reason=f'return_act:{act_id}', from_act=True)
    if owner['code_id'] is not None:
        raise ValueError('Не удалось сверить привязку Честного знака для акта')
    # Full code/PDF may have expired; its durable reservation can still be released.
    conn.execute("UPDATE marking_registry SET state='released',revision=revision+1,updated_at=? WHERE fingerprint=?",
                 (db.now_iso(), owner['fingerprint']))
    db.log_event('marking_unbound', account_id=owner['account_id'], user=user, posting_number=owner['order_id'],
                 message=f'Честный знак отвязан по акту {act_id}; срок хранения полного кода истёк', conn=conn)
    return True


def history(archive_id):
    return [dict(r) for r in db.query('SELECT h.* FROM marking_releases h WHERE h.archive_id=? '
             'AND h.id=(SELECT MAX(x.id) FROM marking_releases x WHERE x.archive_id=h.archive_id AND x.fingerprint=h.fingerprint) '
             'AND NOT EXISTS(SELECT 1 FROM marking_codes c JOIN marking_orders m ON m.id=c.marking_order_id '
             'WHERE m.archive_id=h.archive_id AND c.fingerprint=h.fingerprint AND c.active=1) ORDER BY h.id', (archive_id,))]
