"""Released Ozon codes can be rebound once; archive history and rights survive."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading

import pytest
from fastapi.testclient import TestClient

from app.core import access, accounts, db, marking, marking_registry, packing, security, sticker_archive
from app.main import app
from app.markets.ozon import marking_returns, returns, store, sync
from app.markets.ozon.client import OzonError
from tests.core.test_marking import BARCODE, code, finish_job, marked_ozon, scan
from tests.core.test_sticker_archive import pdf, sign_in
from tests.core.test_sticker_search import add_mark


@pytest.fixture
def bound(account):
    archive, mark_id = add_mark(account, 'original-order', code())
    saved = marking.order(account['id'], 'original-order')
    db.execute("UPDATE marking_orders SET status='accepted' WHERE id=?", (saved['id'],))
    db.execute("UPDATE marking_codes SET status='accepted' WHERE id=?", (mark_id,))
    return archive, mark_id, saved


def accepted_order(account, user, quantity=1):
    api, number, sku = marked_ozon(account, quantity=quantity)
    for n in range(quantity):
        scan(account, user, BARCODE)
        scan(account, user, code(f'SERIAL{n:07}'))
    finish_job(account, number)
    saved = marking.order(account['id'], number)
    assert saved['status'] == 'accepted'
    sticker_archive.save(saved['archive_id'], pdf())
    packing.release(user)
    db.execute("UPDATE postings SET local_state='packed' WHERE account_id=? AND posting_number=?", (account['id'], number))
    return api, number, sku, saved


def target(account, user, api, previous):
    raw = deepcopy(api._postings[previous])
    raw.update(posting_number='90000000-0001-1', order_number='90000000-0001', order_id=90000000,
               status='awaiting_packaging', substatus='posting_created', product_exemplars=None)
    raw['products'] = [dict(raw['products'][0], quantity=1)]
    api._postings[raw['posting_number']] = raw
    with db.write() as conn:
        store.upsert_posting(conn, account['id'], raw)
    opened = scan(account, user, BARCODE)
    assert opened['state']['active']['posting_number'] == raw['posting_number']
    return raw['posting_number']


def post_unbind(client, archive, mark_id, csrf, version=None):
    detail = client.get(f'/api/stickers/{archive}/marking').json()
    version = version or detail['items'][0]['unbind_version']
    return client.post(f'/api/stickers/{archive}/marking/{mark_id}/unbind',
                       json={'version': version}, headers={'X-CSRF-Token': csrf})


def test_admin_unbind_preserves_sticker_history_pdf_and_search(account, bound):
    archive, mark_id, saved = bound
    before = db.query_one('SELECT pdf FROM sticker_versions WHERE archive_id=?', (archive,))['pdf']
    with TestClient(app, follow_redirects=False) as client:
        csrf = sign_in(client)
        data = client.get(f'/api/stickers/{archive}/marking').json()
        assert data['can_unbind'] and data['items'][0]['can_unbind']
        response = post_unbind(client, archive, mark_id, csrf)
        assert response.status_code == 200, response.text
        entry = response.json()['items'][0]
        assert entry['history'] and entry['status'] == 'detached' and not entry['can_unbind']
        assert client.get(f'/api/stickers/marking/history/{entry["id"]}.pdf').status_code == 200
        assert client.get(f'/api/stickers/{archive}/marking.pdf').status_code == 200
    assert db.query_one('SELECT pdf FROM sticker_versions WHERE archive_id=?', (archive,))['pdf'] == before
    assert sticker_archive.search(q=code())[0][0]['id'] == archive
    assert sticker_archive.search(q=code()[2:16])[0][0]['marking_count'] == 1
    assert not marking.codes(saved)
    assert db.query_one('SELECT COUNT(*) AS n FROM events WHERE kind=?', ('marking_unbound',))['n'] == 1


@pytest.mark.parametrize('role,sections,allowed', [
    (access.ADMIN, '["stickers"]', True), (access.PACKER, '["stickers"]', False), (access.ADMIN, '[]', False),
])
def test_unbind_permissions_are_checked_on_server(bound, role, sections, allowed):
    archive, mark_id, _ = bound
    db.execute('INSERT INTO users(login,password_hash,role,sections,active,created_at) VALUES(?,?,?,?,1,?)',
               ('operator', security.hash_password('reader-password'), role, sections, db.now_iso()))
    version = marking.archive_details(archive)['items'][0]['unbind_version']
    with TestClient(app, follow_redirects=False) as client:
        csrf = sign_in(client, 'operator', 'reader-password')
        if not csrf:
            csrf = security.read_session(client.cookies.get(security.SESSION_COOKIE))['csrf']
        response = client.post(f'/api/stickers/{archive}/marking/{mark_id}/unbind',
                               json={'version': version}, headers={'X-CSRF-Token': csrf})
        assert response.status_code == (200 if allowed else 403)


def test_unbind_requires_csrf_and_current_binding_version(bound):
    archive, mark_id, _ = bound
    with TestClient(app, follow_redirects=False) as client:
        sign_in(client)
        assert client.post(f'/api/stickers/{archive}/marking/{mark_id}/unbind', json={'version': 'x'}).status_code == 403
        csrf = sign_in(client)
        assert post_unbind(client, archive, mark_id, csrf, version='stale').status_code == 409
    assert marking.codes(bound[2])


def test_stale_button_cannot_unbind_a_new_binding_in_the_same_second(account, user, bound, monkeypatch):
    archive, mark_id, saved = bound
    version = marking.archive_details(archive)['items'][0]['unbind_version']
    monkeypatch.setattr(db, 'now_iso', lambda: '2026-11-20T12:00:00+00:00')
    marking_registry.release(archive, mark_id, user=user, expected_version=version)
    with db.write() as conn:
        conn.execute("UPDATE marking_codes SET active=1,status='accepted' WHERE id=?", (mark_id,))
        fingerprint = conn.execute('SELECT fingerprint FROM marking_codes WHERE id=?', (mark_id,)).fetchone()['fingerprint']
        marking_registry.claim(conn, fingerprint, account, saved, mark_id)
    with pytest.raises(ValueError, match='изменилась'):
        marking_registry.release(archive, mark_id, user=user, expected_version=version)
    assert marking.codes(saved)


@pytest.mark.parametrize('value', [None, [], '', 'false'])
def test_malformed_cancellation_cannot_free_a_code(account, bound, value):
    archive, _, saved = bound
    assert not marking_returns.cancelled(account, {'posting_number': saved['order_id'], 'status': 'cancelled', 'cancellation': value})
    assert marking.codes(saved)


@pytest.mark.parametrize('phase', ['queued', 'checking', 'uncertain'])
def test_unbind_does_not_interrupt_inflight_transmission(bound, phase):
    archive, mark_id, saved = bound
    db.execute('UPDATE marking_orders SET status=? WHERE id=?', (phase, saved['id']))
    with pytest.raises(ValueError, match='Дождитесь'):
        marking_registry.release(archive, mark_id)
    assert len(marking.codes(saved)) == 1


def test_yandex_is_not_automatically_or_manually_released(yandex_account):
    archive, mark_id = add_mark(yandex_account, 'yandex-order', code())
    with pytest.raises(LookupError, match='Ozon'):
        marking_registry.release(archive, mark_id)


def test_manual_release_rebinds_and_preserves_old_history(account, user, bound):
    archive, mark_id, _ = bound
    marking_registry.release(archive, mark_id, user=user)
    api, number, _ = marked_ozon(account)
    scan(account, user, BARCODE)
    result = scan(account, user, code())
    assert result['action'] == 'marking_saved', result
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'
    old = marking.archive_details(archive)['items']
    assert len(old) == 1 and old[0]['history']
    assert marking.order(account['id'], number)['id'] == db.query_one('SELECT marking_order_id FROM marking_codes WHERE id=?', (mark_id,))['marking_order_id']
    assert api.marking_requests[-1]['products'][0]['exemplars'][0]['marks'][0]['mark'] == code()
    with pytest.raises(LookupError):
        marking_registry.release(archive, mark_id)


@pytest.mark.parametrize('flag', [None, True, False, 'false', 0])
def test_cancel_check_requires_explicit_before_assembly_boolean(account, user, flag):
    api, number, _, saved = accepted_order(account, user)
    api._postings[number].update(status='cancelled', cancellation={'cancelled_after_ship': flag})
    new_number = target(account, user, api, number)
    result = scan(account, user, code('SERIAL0000000'))
    if flag is False:
        assert result['action'] == 'marking_saved', result
        finish_job(account, new_number)
        assert marking.order(account['id'], new_number)['status'] == 'accepted'
    else:
        assert result['message'] == f'Данный Честный знак уже используется в заказе: {number}'
        assert len(marking.codes(saved)) == 1


def test_sync_releases_definitely_cancelled_order_without_reshipping(account, user):
    api, number, _, saved = accepted_order(account, user)
    api._postings[number].update(status='cancelled', cancellation={'cancelled_after_ship': False})
    sync.sync_postings(account)
    assert not marking.codes(saved)
    assert len(marking_registry.history(saved['archive_id'])) == 1


@pytest.mark.parametrize('accepted,status,quantity,original,expected', [
    (True, 'ReceivedBySeller', 1, 1, True),
    (False, 'ReceivedBySeller', 1, 1, False),
    (True, 'ArrivedAtReturnPlace', 1, 1, False),
    (True, 'ReceivedBySeller', 1, 2, False),
    (True, 'ReceivedBySeller', 2, 2, True),
])
def test_return_requires_received_and_accepted_entire_sku(account, user, accepted, status, quantity, original, expected):
    api, number, sku, saved = accepted_order(account, user, original)
    api._postings[number]['status'] = 'delivered'
    raw = {'id': 12345, 'schema': 'Fbs', 'posting_number': number,
           'product': {'sku': int(sku), 'quantity': quantity},
           'visual': {'status': {'sys_name': status}}}
    api._returns = [raw]
    with db.write() as conn:
        returns.upsert_return(conn, account['id'], raw)
    if accepted:
        db.execute("UPDATE returns SET mark='ok' WHERE account_id=? AND id='12345'", (account['id'],))
    target(account, user, api, number)
    result = scan(account, user, code('SERIAL0000000'))
    assert (result['action'] == 'marking_saved') is expected, result
    if not expected:
        assert result['message'] == f'Данный Честный знак уже используется в заказе: {number}'


def test_unavailable_ozon_does_not_free_code_or_expose_error(account, user, monkeypatch):
    api, number, _, saved = accepted_order(account, user)
    target(account, user, api, number)
    monkeypatch.setattr(api, 'posting_get', lambda *_a, **_k: (_ for _ in ()).throw(OzonError('private payload', status=403)))
    # Invoke the binding check directly; the current order's own API checks
    # are separate and should retain their existing error handling.
    owner = marking_registry.binding(marking._fingerprint(marking.marking_codes.parse(code('SERIAL0000000'))))
    assert not marking_returns.try_release(owner)
    assert len(marking.codes(saved)) == 1


def test_registry_and_release_survive_restart_and_archive_expiry(account, user, bound):
    archive, mark_id, _ = bound
    db.init_db()
    marking_registry.release(archive, mark_id, user=user)
    db.init_db()
    assert db.query_one('SELECT state FROM marking_registry')['state'] == 'released'
    db.execute("UPDATE sticker_orders SET created_at='2000-01-01' WHERE id=?", (archive,))
    db.execute("UPDATE sticker_versions SET saved_at='2000-01-01' WHERE archive_id=?", (archive,))
    sticker_archive.purge()
    assert not db.query('SELECT * FROM marking_codes')
    assert db.query_one('SELECT state,code_id FROM marking_registry')['state'] == 'released'
    marked_ozon(account)
    scan(account, user, BARCODE)
    assert scan(account, user, code())['action'] == 'marking_saved'


def test_unreleased_code_stays_reserved_after_archive_expiry(account, user, bound):
    archive, _, _ = bound
    db.init_db()
    db.execute("UPDATE sticker_orders SET created_at='2000-01-01' WHERE id=?", (archive,))
    db.execute("UPDATE sticker_versions SET saved_at='2000-01-01' WHERE archive_id=?", (archive,))
    sticker_archive.purge()
    marked_ozon(account)
    scan(account, user, BARCODE)
    result = scan(account, user, code())
    assert result['message'] == 'Данный Честный знак уже используется в заказе: original-order'


@pytest.mark.parametrize('released', [False, True])
def test_two_workers_cannot_claim_the_same_code(account, user, other_user, bound, monkeypatch, released):
    archive, mark_id, _ = bound
    if released:
        marking_registry.release(archive, mark_id, user=user)
    else:
        db.execute('DELETE FROM marking_codes')
        db.execute('DELETE FROM marking_registry')
    second = accounts.get(accounts.create('ozon', 'Second', 'test-client-2', 'test-key-2'))
    marked_ozon(account)
    marked_ozon(second)
    scan(account, user, BARCODE)
    scan(second, other_user, BARCODE)
    barrier = threading.Barrier(2)
    original = marking._store_scan

    def simultaneous(*args, **kwargs):
        barrier.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(marking, '_store_scan', simultaneous)
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(scan, shop, worker, code()) for shop, worker in [(account, user), (second, other_user)]]
        results = [task.result(timeout=15) for task in tasks]
    assert sum(r.get('action') == 'marking_saved' for r in results) == 1, results
    assert sum(r['status'] == 'error' for r in results) == 1
    assert db.query_one('SELECT COUNT(*) AS n FROM marking_codes WHERE active=1')['n'] == 1
    owner = db.query_one('SELECT * FROM marking_registry')
    row = db.query_one('SELECT m.account_id FROM marking_codes c JOIN marking_orders m ON m.id=c.marking_order_id WHERE c.active=1')
    assert owner['account_id'] == row['account_id']
