"""Membership in a return act releases that order's codes on every platform."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core import accounts, db, marking, marking_registry, marking_returns, return_acts, security, sticker_archive
from app.core import store as core_store
from app.main import app
from app.markets import registry
from app.markets.ozon import returns as ozon_returns
from app.markets.yandex import returns as yandex_returns
from tests.core.test_marking import BARCODE, code, finish_job, marked_ozon, scan
from tests.core.test_sticker_archive import sign_in
from tests.core.test_sticker_search import add_mark


def receipt(account, number, rid='12345'):
    source = registry.require(account['marketplace']).returns
    with db.write() as conn:
        if account['marketplace'] == 'ozon':
            ozon_returns.upsert_return(conn, account['id'], {
                'id': int(rid), 'schema': 'Fbs', 'posting_number': number,
                'product': {'sku': 10, 'quantity': 1},
                'visual': {'status': {'sys_name': 'ReceivedBySeller'}}})
        elif account['marketplace'] == 'yandex':
            yandex_returns.upsert_return(conn, account['id'], '9000001', {
                'id': rid, 'orderId': number, 'shipmentStatus': 'PICKED', 'updateDate': db.now_iso(),
                'items': [{'shopSku': 'sku', 'count': 1}]})
        else:
            conn.execute('INSERT INTO avito_orders(account_id,id,status,received_at,received_day) VALUES(?,?,?,?,?)',
                         (account['id'], number, 'on_return', db.now_iso(), core_store.local_day()))
    return source, (number if account['marketplace'] == 'avito' else rid)


def make_act(source, account, user):
    result = return_acts.from_received(source, account['id'], core_store.local_day(), user=user)
    assert result['status'] == 'ok', result
    return result['act_id']


@pytest.mark.parametrize('market', ['ozon', 'yandex', 'avito'])
def test_act_immediately_detaches_all_order_codes_without_marks(account, yandex_account, avito_account, user, market):
    shop = {'ozon': account, 'yandex': yandex_account, 'avito': avito_account}[market]
    archive, first = add_mark(shop, 'returned-order', code())
    _, second = add_mark(shop, 'returned-order', code('ABCDEF1234568'), archive_id=archive)
    # A single returned position is enough: the agreed rule covers the whole order.
    db.execute("UPDATE marking_codes SET item_key='other-sku' WHERE id=?", (second,))
    saved = marking.order(shop['id'], 'returned-order')
    db.execute("UPDATE marking_orders SET status='uncertain' WHERE id=?", (saved['id'],))
    source, rid = receipt(shop, 'returned-order')
    act = make_act(source, shop, user)
    assert return_acts.get(act)['confirmed_at'] is None
    assert db.query_one(f'SELECT mark FROM {source.table} WHERE account_id=? AND id=?', (shop['id'], rid))['mark'] is None
    assert not marking.codes(saved)
    assert {r['state'] for r in db.query('SELECT state FROM marking_registry')} == {'released'}
    detail = marking.archive_details(archive)
    assert len(detail['items']) == 2 and all(r['history'] and r['status'] == 'detached' for r in detail['items'])
    assert marking.print_pdf([], history_ids=[r['id'] for r in detail['items']]).startswith(b'%PDF')
    assert sticker_archive.search(q=code()[2:16])[0][0]['marking_count'] == 2
    _, number, _ = marked_ozon(account)
    scan(account, user, BARCODE)
    assert scan(account, user, code())['action'] == 'marking_saved'
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'
    assert all(r['status'] == 'detached' for r in marking.archive_details(archive)['items'])


def test_old_hidden_replaced_code_is_released_too(account, user):
    archive, first = add_mark(account, 'legacy-order', code())
    add_mark(account, 'legacy-order', code('ABCDEF1234568'), archive_id=archive)
    db.execute("UPDATE marking_codes SET active=0,status='error' WHERE id=?", (first,))
    source, _ = receipt(account, 'legacy-order')
    make_act(source, account, user)
    assert all(r['state'] == 'released' for r in db.query('SELECT state FROM marking_registry'))
    detail = marking.archive_details(archive)
    assert len(detail['items']) == 2
    assert all(r['status'] == 'detached' for r in detail['items'])


def test_account_and_market_are_part_of_order_identity(account, yandex_account, user):
    other = accounts.get(accounts.create('ozon', 'Other shop', 'test-client-2', 'test-key-2'))
    one, _ = add_mark(account, 'same-number', code())
    two, _ = add_mark(other, 'same-number', code('ABCDEF1234568'))
    three, _ = add_mark(yandex_account, 'same-number', code('ABCDEF1234569'))
    source, _ = receipt(account, 'same-number')
    make_act(source, account, user)
    assert marking.archive_details(one)['items'][0]['status'] == 'detached'
    assert not marking.archive_details(two)['items'][0]['history']
    assert not marking.archive_details(three)['items'][0]['history']


def test_return_without_an_act_does_not_detach_codes(account, user):
    archive, _ = add_mark(account, 'not-in-act', code())
    receipt(account, 'not-in-act')
    marking_returns.process_pending()
    assert not marking.archive_details(archive)['items'][0]['history']


def test_act_membership_releases_even_a_row_marked_bad(account, user):
    archive, _ = add_mark(account, 'all-rows-order', code())
    source, rid = receipt(account, 'all-rows-order')
    db.execute("UPDATE returns SET mark='bad' WHERE account_id=? AND id=?", (account['id'], rid))
    make_act(source, account, user)
    assert marking.archive_details(archive)['items'][0]['status'] == 'detached'


def test_ozon_order_reference_resolves_missing_posting_number(account, user):
    _, number, _ = marked_ozon(account)
    archive, _ = add_mark(account, number, code())
    source, rid = receipt(account, number)
    posting = db.query_one('SELECT order_id,order_number FROM postings WHERE account_id=? AND posting_number=?', (account['id'], number))
    db.execute('UPDATE returns SET posting_number=NULL,order_id=?,order_number=? WHERE account_id=? AND id=?',
               (posting['order_id'], posting['order_number'], account['id'], rid))
    make_act(source, account, user)
    assert marking.archive_details(archive)['items'][0]['status'] == 'detached'


def test_dry_run_does_not_unbind_codes(account):
    archive, _ = add_mark(account, 'dry-run-order', code())
    receipt(account, 'dry-run-order')
    with TestClient(app, follow_redirects=False) as client:
        csrf = sign_in(client)
        result = client.post('/api/returns/acts/by-day', json={'shop': str(account['id']), 'day': core_store.local_day(), 'dry_run': True},
                             headers={'X-CSRF-Token': csrf})
        assert result.status_code == 200 and result.json()['found'] == 1
    assert not db.query('SELECT * FROM marking_return_jobs')
    assert not marking.archive_details(archive)['items'][0]['history']


def test_act_unbinding_does_not_call_marketplace_api(account, user, monkeypatch):
    from app.markets.ozon import client

    archive, _ = add_mark(account, 'offline-order', code())
    source, _ = receipt(account, 'offline-order')

    def forbidden(*args, **kwargs):
        pytest.fail('return act unbinding must not depend on a marketplace API call')

    monkeypatch.setattr(client, 'get_client', forbidden)
    make_act(source, account, user)
    assert marking.archive_details(archive)['items'][0]['status'] == 'detached'


def test_packer_can_trigger_unbinding_by_creating_an_act(account):
    archive, _ = add_mark(account, 'packer-act', code())
    receipt(account, 'packer-act')
    db.execute('INSERT INTO users(login,password_hash,role,sections,active,created_at) VALUES(?,?,?,?,1,?)',
               ('receiver', security.hash_password('reader-password'), 'packer', '["returns","stickers"]', db.now_iso()))
    with TestClient(app, follow_redirects=False) as client:
        csrf = sign_in(client, 'receiver', 'reader-password')
        result = client.post('/api/returns/acts/by-day', json={'shop': str(account['id']), 'day': core_store.local_day()},
                             headers={'X-CSRF-Token': csrf})
        assert result.status_code == 200, result.text
        assert result.json()['status'] == 'ok'
        entries = client.get(f'/api/stickers/{archive}/marking').json()['items']
        assert len(entries) == 1 and entries[0]['status'] == 'detached'
    assert db.query_one("SELECT login FROM events WHERE kind='marking_unbound'")['login'] == 'receiver'


def busy_order(account, number):
    db.execute('INSERT INTO order_operations VALUES(?,?,?,?)', (account['id'], number, 'other-process', '2099-01-01T00:00:00+00:00'))


def unlock(account, number):
    db.execute('DELETE FROM order_operations WHERE account_id=? AND order_id=?', (account['id'], number))


def test_busy_order_has_a_durable_retry_job(account, user):
    archive, _ = add_mark(account, 'busy-order', code())
    source, _ = receipt(account, 'busy-order')
    busy_order(account, 'busy-order')
    act = make_act(source, account, user)
    assert db.query_one('SELECT status FROM marking_return_jobs')['status'] == 'pending'
    assert not marking.archive_details(archive)['items'][0]['history']
    unlock(account, 'busy-order')
    marking.run_once()
    assert marking.archive_details(archive)['items'][0]['status'] == 'detached'
    assert db.query_one('SELECT status FROM marking_return_jobs')['status'] == 'applied'
    assert marking_returns.process_act(act) == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM events WHERE kind='marking_unbound'")['n'] == 1


def test_old_job_cannot_detach_a_newer_binding(account, user):
    archive, mark_id = add_mark(account, 'rebound-order', code())
    saved = marking.order(account['id'], 'rebound-order')
    source, _ = receipt(account, 'rebound-order')
    busy_order(account, 'rebound-order')
    act = make_act(source, account, user)
    unlock(account, 'rebound-order')
    marking_registry.release(archive, mark_id, user=user)
    with db.write() as conn:
        conn.execute("UPDATE marking_codes SET active=1,status='accepted' WHERE id=?", (mark_id,))
        fingerprint = conn.execute('SELECT fingerprint FROM marking_codes WHERE id=?', (mark_id,)).fetchone()['fingerprint']
        marking_registry.claim(conn, fingerprint, account, saved, mark_id)
    assert marking_returns.process_act(act) == 0
    assert marking.codes(saved)


def test_deleted_pending_act_does_not_release_codes(account, user):
    archive, _ = add_mark(account, 'deleted-act-order', code())
    source, _ = receipt(account, 'deleted-act-order')
    busy_order(account, 'deleted-act-order')
    act = make_act(source, account, user)
    unlock(account, 'deleted-act-order')
    db.execute('DELETE FROM return_acts WHERE id=?', (act,))
    marking_returns.process_act(act)
    assert db.query_one('SELECT status FROM marking_return_jobs')['status'] == 'cancelled'
    assert not marking.archive_details(archive)['items'][0]['history']


def test_old_act_cannot_release_code_used_for_a_new_order(account, user):
    archive, mark_id = add_mark(account, 'old-order', code())
    source, _ = receipt(account, 'old-order')
    busy_order(account, 'old-order')
    act = make_act(source, account, user)
    unlock(account, 'old-order')
    marking_registry.release(archive, mark_id, user=user)
    _, number, _ = marked_ozon(account)
    scan(account, user, BARCODE)
    assert scan(account, user, code())['action'] == 'marking_saved'
    finish_job(account, number)
    marking_returns.process_act(act)
    assert marking.codes(marking.order(account['id'], number))


def test_existing_acts_are_processed_once_on_upgrade(account, user):
    archive, _ = add_mark(account, 'already-returned', code())
    source, rid = receipt(account, 'already-returned')
    db.execute('INSERT INTO return_acts(id,created_at,created_by,kind,account_id,received_day,day_seq) VALUES(?,?,?,?,?,?,1)',
               ('old-act', db.now_iso(), user['login'], return_acts.BY_DAY, account['id'], core_store.local_day()))
    db.execute(f'UPDATE {source.table} SET act_id=? WHERE account_id=? AND id=?', ('old-act', account['id'], rid))
    db.execute('DELETE FROM kv WHERE key=?', (marking_returns.MIGRATED,))
    db.init_db()
    assert marking.archive_details(archive)['items'][0]['status'] == 'detached'
    db.init_db()
    assert db.query_one('SELECT COUNT(*) AS n FROM marking_releases')['n'] == 1


def test_job_rolls_back_all_codes_on_failure_and_can_retry(account, user, monkeypatch):
    archive, _ = add_mark(account, 'atomic-order', code())
    add_mark(account, 'atomic-order', code('ABCDEF1234568'), archive_id=archive)
    source, _ = receipt(account, 'atomic-order')
    busy_order(account, 'atomic-order')
    act = make_act(source, account, user)
    unlock(account, 'atomic-order')
    original = marking_registry.release_for_act
    calls = []

    def fail_second(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError('simulated crash')
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(marking_registry, 'release_for_act', fail_second)
        with pytest.raises(RuntimeError, match='simulated crash'):
            marking_returns.process_act(act)
    assert not db.query('SELECT * FROM marking_releases')
    assert len(marking.codes(marking.order(account['id'], 'atomic-order'))) == 2
    assert db.query_one('SELECT status FROM marking_return_jobs')['status'] == 'pending'
    assert marking_returns.process_act(act) == 2


def test_expired_code_reservation_can_be_freed_by_an_act(account, user):
    archive, _ = add_mark(account, 'expired-order', code())
    db.init_db()
    db.execute("UPDATE sticker_orders SET created_at='2000-01-01' WHERE id=?", (archive,))
    db.execute("UPDATE sticker_versions SET saved_at='2000-01-01' WHERE archive_id=?", (archive,))
    sticker_archive.purge()
    assert db.query_one('SELECT code_id FROM marking_registry')['code_id'] is None
    source, _ = receipt(account, 'expired-order')
    make_act(source, account, user)
    assert db.query_one('SELECT state FROM marking_registry')['state'] == 'released'
