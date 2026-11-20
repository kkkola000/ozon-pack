"""All orders in a return act release their captured marking bindings once."""
from __future__ import annotations

import json
import logging

from . import db, order_lock

log = logging.getLogger(__name__)
MIGRATED = 'marking_return_acts_migrated_v1'
SCHEMA = """
CREATE TABLE IF NOT EXISTS marking_return_jobs (
 act_id TEXT NOT NULL,
 account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 marketplace TEXT NOT NULL,
 order_id TEXT NOT NULL,
 bindings TEXT NOT NULL,
 actor TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending',
 created_at TEXT NOT NULL,
 checked_at TEXT,
 PRIMARY KEY(act_id,account_id,order_id)
);
CREATE INDEX IF NOT EXISTS idx_marking_return_pending ON marking_return_jobs(status,checked_at);
"""


def enqueue_act(conn, act_id: str, user: dict | None = None) -> int:
    from . import marking_registry, return_acts

    act = conn.execute('SELECT * FROM return_acts WHERE id=?', (act_id,)).fetchone()
    if not act:
        return 0
    count = 0
    for market, source in return_acts.sources():
        if not source.marking_orders:
            continue
        for account_id, order_id in set(source.marking_orders(conn, act_id)):
            marking_registry.backfill(conn, account_id=account_id, order_id=order_id)
            rows = conn.execute("SELECT fingerprint,revision FROM marking_registry WHERE account_id=? AND marketplace=? "
                                "AND order_id=? AND state='bound'", (account_id, market.code, order_id)).fetchall()
            if not rows:
                continue
            snapshots = [{'fingerprint': row['fingerprint'], 'revision': row['revision']} for row in rows]
            count += conn.execute('INSERT OR IGNORE INTO marking_return_jobs(act_id,account_id,marketplace,order_id,bindings,actor,created_at) '
                                  'VALUES(?,?,?,?,?,?,?)', (act_id, account_id, market.code, order_id,
                                  json.dumps(snapshots), (user or {}).get('login') or act['created_by'] or 'Система', db.now_iso())).rowcount
    return count


def backfill(conn):
    if conn.execute('SELECT 1 FROM kv WHERE key=?', (MIGRATED,)).fetchone():
        return
    for act in conn.execute('SELECT id FROM return_acts ORDER BY created_at,id').fetchall():
        enqueue_act(conn, act['id'])
    conn.execute('INSERT INTO kv(key,value) VALUES(?,?)', (MIGRATED, db.now_iso()))


def _process(job) -> int:
    from . import marking_registry

    key = (job['act_id'], job['account_id'], job['order_id'])
    try:
        with order_lock.hold(job['account_id'], job['order_id']), db.write() as conn:
            latest = conn.execute('SELECT * FROM marking_return_jobs WHERE act_id=? AND account_id=? AND order_id=?', key).fetchone()
            if not latest or latest['status'] != 'pending':
                return 0
            if not conn.execute('SELECT 1 FROM return_acts WHERE id=?', (job['act_id'],)).fetchone():
                conn.execute("UPDATE marking_return_jobs SET status='cancelled',checked_at=? WHERE act_id=? AND account_id=? AND order_id=?",
                             (db.now_iso(), *key))
                return 0
            changed = 0
            for snapshot in json.loads(latest['bindings']):
                owner = marking_registry.binding(snapshot['fingerprint'], conn)
                if (not owner or owner['state'] != 'bound' or owner['marketplace'] != latest['marketplace']
                        or not marking_registry.owns(owner, job['account_id'], job['order_id'])
                        or owner['revision'] != snapshot['revision']):
                    continue  # An old act must never detach a newer binding.
                changed += marking_registry.release_for_act(conn, owner, job['act_id'], {'login': latest['actor']})
            conn.execute("UPDATE marking_return_jobs SET status='applied',checked_at=? WHERE act_id=? AND account_id=? AND order_id=?",
                         (db.now_iso(), *key))
            return changed
    except ValueError:
        # In-flight order operations keep the durable job pending for a retry.
        db.execute('UPDATE marking_return_jobs SET checked_at=? WHERE act_id=? AND account_id=? AND order_id=?',
                   (db.now_iso(), *key))
        log.warning('Отвязка Честного знака по акту %s отложена: заказ %s обрабатывается', job['act_id'], job['order_id'])
        return 0


def process_act(act_id: str) -> int:
    return sum(_process(row) for row in db.query("SELECT * FROM marking_return_jobs WHERE act_id=? AND status='pending'", (act_id,)))


def process_order(account_id: int, order_id: str) -> int:
    return sum(_process(row) for row in db.query("SELECT * FROM marking_return_jobs WHERE account_id=? AND order_id=? AND status='pending'",
                                                (account_id, order_id)))


def process_pending(limit: int | None = 50) -> int:
    return sum(_process(row) for row in db.query("SELECT * FROM marking_return_jobs WHERE status='pending' ORDER BY COALESCE(checked_at,''),created_at LIMIT ?",
                                                (-1 if limit is None else limit,)))
