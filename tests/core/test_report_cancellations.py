"""Cancel removes the shipment mark; completed collections and their audit survive."""
import pytest

from app.core import db, packing, report
from app.markets import registry
from tests.core.test_marking import BARCODE, code, finish_job, marked_ozon, marked_yandex, scan


@pytest.fixture(params=['ozon', 'yandex'])
def shop(request, account, yandex_account):
    return account if request.param == 'ozon' else yandex_account


def opened(shop, user):
    _, number, _ = (marked_ozon if shop['marketplace'] == 'ozon' else marked_yandex)(shop)
    assert scan(shop, user, BARCODE)['status'] == 'ok'
    row = dict(db.query_one('SELECT * FROM shipped_items WHERE account_id=? AND posting_number=?', (shop['id'], number)))
    assert row['status'] == 'ok'
    return number, row


def test_cancelled_attempt_is_not_counted_as_shipped_and_keeps_audit(shop, user):
    number, before = opened(shop, user)
    with db.write() as conn:
        report.record_error(conn, shop, user, 'wrong_label', posting_number=number)
    packing.release(user)
    row = dict(db.query_one('SELECT * FROM shipped_items WHERE id=?', (before['id'],)))
    assert row['status'] == 'cancelled' and row['reason'] == 'assembly_cancelled'
    totals = report.totals(before['report_date'], shop['id'])
    assert totals['shipped'] == totals['postings'] == 0
    assert totals['errors'] == 1
    assert report.rows(before['report_date'], shop['id'], 'cancelled')[0]['status_label'] == 'Сборка отменена'
    assert 'Сборка отменена' in report.to_csv(before['report_date'], shop['id']).decode('utf-8-sig')
    assert db.query_one("SELECT id FROM events WHERE account_id=? AND posting_number=? AND kind IN ('pack_release','yandex_pack_release')",
                        (shop['id'], number))


def test_reopening_after_cancel_counts_the_rescanned_unit_once(shop, user):
    _, original = opened(shop, user)
    packing.release(user)
    assert scan(shop, user, BARCODE)['status'] == 'ok'
    rows = db.query("SELECT * FROM shipped_items WHERE account_id=? AND status<>'error'", (shop['id'],))
    assert len(rows) == 1 and rows[0]['id'] == original['id'] and rows[0]['status'] == 'ok'
    assert report.totals(rows[0]['report_date'], shop['id'])['shipped'] == 1


def test_old_false_shipment_marks_are_repaired_from_cancellation_events(shop, user):
    _, original = opened(shop, user)
    packing.release(user)
    db.execute("UPDATE shipped_items SET status='ok',reason=NULL WHERE id=?", (original['id'],))
    db.init_db()
    assert db.query_one('SELECT status FROM shipped_items WHERE id=?', (original['id'],))['status'] == 'cancelled'
    db.init_db()
    assert report.totals(original['report_date'], shop['id'])['shipped'] == 0


def test_completed_shipment_is_not_changed_by_later_release_events(shop, user):
    number, original = opened(shop, user)
    scan(shop, user, code())
    finish_job(shop, number)
    assert scan(shop, user, number)['action'] == 'completed'
    # Even a manual reset of local state must not erase actual completion history.
    board = registry.get(shop['marketplace']).orders
    db.execute(f"UPDATE {board.table} SET local_state='new' WHERE account_id=? AND {board.key}=?", (shop['id'], number))
    db.log_event('pack_release' if shop['marketplace'] == 'ozon' else 'yandex_pack_release',
                 account_id=shop['id'], user=user, posting_number=number)
    db.init_db()
    assert db.query_one('SELECT status FROM shipped_items WHERE id=?', (original['id'],))['status'] == 'ok'
    assert report.totals(original['report_date'], shop['id'])['shipped'] == 1
