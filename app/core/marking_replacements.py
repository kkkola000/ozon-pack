"""Release superseded reservations only after a fresh, complete remote proof."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import accounts, db, exemplar_data, marking, marking_registry, order_lock

log = logging.getLogger(__name__)
CANDIDATES = """
 SELECT old.*,r.revision AS owner_revision,new.id AS replacement_id
 FROM marking_codes old
 JOIN marking_registry r ON r.fingerprint=old.fingerprint AND r.code_id=old.id
 JOIN marking_orders m ON m.id=old.marking_order_id
 JOIN marking_codes new ON new.marking_order_id=old.marking_order_id
   AND new.item_key=old.item_key AND new.unit_no=old.unit_no
   AND new.active=1 AND new.status='accepted'
 WHERE old.active=0 AND old.status IN ('error','removed') AND r.state='bound'
   AND r.account_id=m.account_id AND r.order_id=m.order_id AND m.status='accepted'
"""


def reconcile(account: dict, order_id: str) -> int:
    """Safe to retry, including after a crash and while repairing old databases."""
    try:
        with order_lock.hold(account['id'], order_id):
            saved = marking.order(account['id'], order_id)
            provider = marking.source(account)
            if not saved or saved['status'] != 'accepted' or not provider or not provider.replacement_snapshot:
                return 0
            candidates = [dict(row) for row in db.query(CANDIDATES + ' AND m.id=?', (saved['id'],))]
            if not candidates:
                return 0
            try:
                marks = marking.codes(saved, full=True)
                if not marks or any(m['status'] != 'accepted' for m in marks):
                    return 0
                remote = provider.replacement_snapshot(account, order_id, exemplar_data.context(saved), marks)
                if remote is None or not {m['fingerprint'] for m in marks}.issubset(remote):
                    return 0
                with db.write() as conn:
                    current = conn.execute('SELECT status,revision FROM marking_orders WHERE id=?', (saved['id'],)).fetchone()
                    if not current or current['status'] != 'accepted' or current['revision'] != saved['revision']:
                        return 0
                    return sum(marking_registry.release_replaced(conn, saved, row)
                               for row in candidates if row['fingerprint'] not in remote)
            finally:
                db.execute('UPDATE marking_orders SET replacements_checked_at=? WHERE id=?', (db.now_iso(), saved['id']))
    except Exception as exc:
        # Do not expose codes/API keys, or change accepted state when cleanup fails.
        log.warning('Сверка заменённых Честных знаков: кабинет %s, заказ %s, %s',
                    account['id'], order_id, type(exc).__name__)
        return 0


def run_once() -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    rows = db.query('SELECT o.account_id,o.order_id FROM marking_orders o '
                    "WHERE o.status='accepted' AND (o.replacements_checked_at IS NULL OR o.replacements_checked_at<?) "
                    'AND EXISTS (' + CANDIDATES + ' AND old.marking_order_id=o.id) '
                    "ORDER BY COALESCE(o.replacements_checked_at,'') LIMIT 5", (cutoff,))
    for row in rows:
        account = accounts.get(row['account_id'])
        if account and account['active'] and accounts.is_configured(account):
            reconcile(account, row['order_id'])
