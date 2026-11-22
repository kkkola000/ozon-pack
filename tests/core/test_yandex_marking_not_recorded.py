"""Closed Yandex layouts keep local CZ without pretending remote acceptance."""
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.core import db, marking, marking_codes, marking_registry, packing
from app.main import app
from app.markets.yandex import store
from tests.core.test_api import login
from tests.core.test_marking import BARCODE, code, finish_job, marked_ozon, marked_yandex, scan


def ready_order(account, quantity=1):
    api, number, key = marked_yandex(account, quantity)
    api._orders[number]['substatus'] = 'READY_TO_SHIP'
    with db.write() as conn:
        store.upsert_yandex_order(conn, account['id'], api._orders[number])
    return api, number, key


def journal():
    return db.query("SELECT * FROM events WHERE kind='marking_not_recorded'")


def test_ready_order_keeps_full_local_code_archive_reservation_and_error_event(yandex_account, user, monkeypatch):
    api, number, key = ready_order(yandex_account)
    write = Mock(side_effect=AssertionError('Cannot write a ready layout'))
    monkeypatch.setattr(api, 'order_boxes', write)
    scan(yandex_account, user, BARCODE)
    assert scan(yandex_account, user, code())['action'] == 'marking_saved'
    finish_job(yandex_account, number)
    saved = marking.order(yandex_account['id'], number)
    mark = marking.codes(saved, full=True)[0]
    assert saved['status'] == mark['status'] == 'not_recorded'
    assert saved['error'] == marking.NOT_RECORDED
    assert mark['raw'] == code() and mark['code'] != code()
    assert marking_registry.binding(mark['fingerprint'])['state'] == 'bound'
    archive = marking.archive_details(saved['archive_id'])
    assert archive['items'][0]['status'] == 'not_recorded'
    assert archive['items'][0]['printable']
    assert marking.print_pdf([mark['id']]).startswith(b'%PDF')
    events = journal()
    assert len(events) == 1
    event = events[0]
    assert event['level'] == 'error' and event['message'] == marking.NOT_RECORDED
    assert (event['account_id'], event['posting_number'], event['user_id'], event['login']) == (
        yandex_account['id'], number, user['id'], user['login'])
    assert code() not in str(dict(event)) and mark['serial'] not in str(dict(event))
    state = packing.state(user)
    assert state['items'][0]['marking']['status'] == 'error'
    assert state['items'][0]['marking']['title'] == marking.NOT_RECORDED
    assert state['complete'] and state['marking']['local_only']
    assert state['marking']['message'] == marking.LOCAL_ONLY_MESSAGE
    with pytest.raises(ValueError, match='отсканировав стикер отправления'):
        packing.complete(user)
    assert scan(yandex_account, user, number)['action'] == 'completed'
    assert marking.order(yandex_account['id'], number)['status'] == 'not_recorded'
    write.assert_not_called()
    assert api._orders[number]['substatus'] == 'READY_TO_SHIP'


def test_repeated_checks_and_same_scan_do_not_duplicate_error_or_resend(yandex_account, user, monkeypatch):
    api, number, key = ready_order(yandex_account)
    write = Mock(side_effect=AssertionError('No write'))
    monkeypatch.setattr(api, 'order_boxes', write)
    scan(yandex_account, user, BARCODE)
    scan(yandex_account, user, code())
    for _ in range(4):
        marking.process(yandex_account, number, force=True)
    assert len(journal()) == 1
    assert scan(yandex_account, user, code())['status'] == 'warning'
    assert len(marking.codes(marking.order(yandex_account['id'], number))) == 1
    assert len(journal()) == 1
    write.assert_not_called()


def test_external_ready_before_dispatch_keeps_code_and_logs_failure(yandex_account, user, monkeypatch):
    api, number, key = marked_yandex(yandex_account)
    scan(yandex_account, user, BARCODE)
    scan(yandex_account, user, code())
    api._orders[number]['substatus'] = 'READY_TO_SHIP'
    write = Mock(side_effect=AssertionError('No write'))
    monkeypatch.setattr(api, 'order_boxes', write)
    finish_job(yandex_account, number)
    assert marking.order(yandex_account['id'], number)['status'] == 'not_recorded'
    assert len(journal()) == 1
    write.assert_not_called()


def test_unknown_previous_write_is_only_reconciled_and_can_later_be_confirmed(yandex_account, user, monkeypatch):
    api, number, key = marked_yandex(yandex_account)
    scan(yandex_account, user, BARCODE)
    scan(yandex_account, user, code())
    write = Mock(return_value={'status': 'OK'})  # Receipt, but no data applied yet.
    monkeypatch.setattr(api, 'order_boxes', write)
    marking.process(yandex_account, number, force=True)
    api._orders[number]['substatus'] = 'READY_TO_SHIP'
    marking.process(yandex_account, number, force=True)
    assert marking.order(yandex_account['id'], number)['status'] == 'not_recorded'
    assert len(journal()) == 1 and write.call_count == 1
    # A later authoritative read is allowed to resolve a delayed result.
    api._orders[number]['items'][0]['instances'] = [{'cisFull': code(), 'cis': marking_codes.short(code())}]
    marking.process(yandex_account, number, force=True)
    saved = marking.order(yandex_account['id'], number)
    assert saved['status'] == 'accepted'
    assert marking.codes(saved)[0]['status'] == 'accepted'
    assert write.call_count == 1 and len(journal()) == 1


@pytest.mark.parametrize('wrong_crypto', [False, True])
def test_existing_remote_code_requires_full_crypto_match(yandex_account, user, monkeypatch, wrong_crypto):
    api, number, key = ready_order(yandex_account)
    remote = code(tail='other-crypto-value') if wrong_crypto else code()
    api._orders[number]['items'][0]['instances'] = [{'cisFull': remote, 'cis': marking_codes.short(remote)}]
    write = Mock(side_effect=AssertionError('Ready order cannot be changed'))
    monkeypatch.setattr(api, 'order_boxes', write)
    scan(yandex_account, user, BARCODE)
    scan(yandex_account, user, code())
    finish_job(yandex_account, number)
    saved = marking.order(yandex_account['id'], number)
    assert saved['status'] == ('not_recorded' if wrong_crypto else 'accepted')
    assert len(journal()) == int(wrong_crypto)
    write.assert_not_called()


def test_only_missing_unit_gets_not_recorded_status(yandex_account, user):
    api, number, key = ready_order(yandex_account, quantity=2)
    api._orders[number]['items'][0]['instances'] = [{'cisFull': code(), 'cis': marking_codes.short(code())}]
    scan(yandex_account, user, BARCODE)
    scan(yandex_account, user, code())
    scan(yandex_account, user, BARCODE)
    scan(yandex_account, user, code('ABCDEF1234568'))
    finish_job(yandex_account, number)
    marks = marking.codes(marking.order(yandex_account['id'], number))
    assert [(m['unit_no'], m['status']) for m in marks] == [(1, 'saved'), (2, 'not_recorded')]
    assert len(journal()) == 1


def test_not_recorded_event_is_visible_in_filtered_journal(yandex_account, user):
    api, number, key = ready_order(yandex_account)
    scan(yandex_account, user, BARCODE)
    scan(yandex_account, user, code())
    finish_job(yandex_account, number)
    with TestClient(app, follow_redirects=False) as client:
        login(client)
        response = client.get('/logs', params={'kind': 'marking_not_recorded', 'level': 'error', 'posting': number})
        assert response.status_code == 200
        assert marking.NOT_RECORDED in response.text and number in response.text
        assert 'badge err' in response.text
        assert code() not in response.text


@pytest.mark.parametrize('change', ['cancelled', 'started', 'quantity', 'campaign'])
def test_local_completion_rechecks_market_state_and_composition(yandex_account, user, change):
    api, number, key = ready_order(yandex_account)
    scan(yandex_account, user, BARCODE)
    scan(yandex_account, user, code())
    finish_job(yandex_account, number)
    assert packing.state(user)['marking']['local_only']
    if change == 'cancelled':
        api._orders[number]['status'] = 'CANCELLED'
    elif change == 'started':
        api._orders[number]['substatus'] = 'STARTED'
    elif change == 'quantity':
        api._orders[number]['items'][0]['count'] = 2
    else:
        api._orders[number]['campaignId'] += 1
    result = scan(yandex_account, user, number)
    assert result['status'] == 'error'
    assert db.query_one('SELECT local_state FROM yandex_orders WHERE account_id=? AND id=?',
                        (yandex_account['id'], number))['local_state'] != 'packed'
    assert marking.order(yandex_account['id'], number)['status'] != 'accepted'


def test_missing_gtd_cannot_use_local_cis_exception(yandex_account, user):
    from tests.core.test_exemplar_data import GTD, enter
    api, number, key = ready_order(yandex_account)
    api._orders[number]['items'][0]['requiredInstanceTypes'] = ['CIS', 'GTD']
    scan(yandex_account, user, BARCODE)
    scan(yandex_account, user, code())
    enter(yandex_account, user, number, key, {'gtd': GTD})
    finish_job(yandex_account, number)
    state = packing.state(user)
    assert state['marking']['status'] == 'not_recorded' and not state['marking']['local_only']
    assert scan(yandex_account, user, number)['status'] == 'error'


def test_already_recorded_gtd_and_local_cis_can_finish_by_sticker(yandex_account, user):
    from tests.core.test_exemplar_data import GTD
    api, number, key = ready_order(yandex_account)
    api._orders[number]['items'][0]['requiredInstanceTypes'] = ['CIS', 'GTD']
    api._orders[number]['items'][0]['instances'] = [{'gtd': GTD}]
    scan(yandex_account, user, BARCODE)
    scan(yandex_account, user, code())
    finish_job(yandex_account, number)
    assert packing.state(user)['marking']['local_only']
    assert scan(yandex_account, user, number)['action'] == 'completed'
    assert marking.order(yandex_account['id'], number)['status'] == 'not_recorded'
    assert len(journal()) == 1


def test_different_remote_cis_does_not_allow_local_override(yandex_account, user):
    api, number, key = ready_order(yandex_account)
    remote = code(tail='different-crypto')
    api._orders[number]['items'][0]['instances'] = [{'cisFull': remote, 'cis': marking_codes.short(remote)}]
    scan(yandex_account, user, BARCODE)
    scan(yandex_account, user, code())
    finish_job(yandex_account, number)
    assert not packing.state(user)['marking']['local_only']
    assert scan(yandex_account, user, number)['status'] == 'error'


def test_short_remote_cis_cannot_prove_local_completion_is_safe(yandex_account, user):
    api, number, key = ready_order(yandex_account)
    api._orders[number]['items'][0]['instances'] = [{'cis': marking_codes.short(code())}]
    scan(yandex_account, user, BARCODE)
    scan(yandex_account, user, code())
    finish_job(yandex_account, number)
    assert not packing.state(user)['marking']['local_only']
    assert scan(yandex_account, user, number)['status'] == 'error'


def test_other_required_data_is_rechecked_before_local_completion(yandex_account, user):
    from tests.core.test_exemplar_data import GTD
    api, number, key = ready_order(yandex_account)
    item = api._orders[number]['items'][0]
    item['requiredInstanceTypes'] = ['CIS', 'GTD']
    item['instances'] = [{'gtd': GTD}]
    scan(yandex_account, user, BARCODE)
    scan(yandex_account, user, code())
    finish_job(yandex_account, number)
    assert packing.state(user)['marking']['local_only']
    item['instances'] = []
    assert scan(yandex_account, user, number)['status'] == 'error'
    assert db.query_one('SELECT local_state FROM yandex_orders WHERE account_id=? AND id=?',
                        (yandex_account['id'], number))['local_state'] != 'packed'


def test_known_invalid_recorded_cis_blocks_local_completion_of_other_unit(yandex_account, user, monkeypatch):
    api, number, key = ready_order(yandex_account, quantity=2)
    api._orders[number]['items'][0]['instances'] = [{'cisFull': code(), 'cis': marking_codes.short(code())}]
    for value in (BARCODE, code(), BARCODE, code('ABCDEF1234568')):
        scan(yandex_account, user, value)
    finish_job(yandex_account, number)
    assert packing.state(user)['marking']['local_only']
    monkeypatch.setattr(api, 'identifiers_status', lambda *_: {'status': 'OK', 'result': {'items': [
        {'id': int(key), 'cis': [{'value': marking_codes.short(code()), 'status': 'INVALID'}]}]}})
    assert scan(yandex_account, user, number)['status'] == 'error'
    assert db.query_one('SELECT local_state FROM yandex_orders WHERE account_id=? AND id=?',
                        (yandex_account['id'], number))['local_state'] != 'packed'


def test_finished_local_order_does_not_keep_polling_or_block_other_jobs(yandex_account, user, monkeypatch):
    api, number, key = ready_order(yandex_account)
    scan(yandex_account, user, BARCODE)
    scan(yandex_account, user, code())
    finish_job(yandex_account, number)
    assert scan(yandex_account, user, number)['action'] == 'completed'
    read = Mock(wraps=api.order_details)
    monkeypatch.setattr(api, 'order_details', read)
    db.execute('UPDATE marking_orders SET checked_at=NULL')
    marking.run_once()
    read.assert_not_called()
    assert marking.order(yandex_account['id'], number)['status'] == 'not_recorded'


def test_locally_finished_code_cannot_be_used_in_another_market(yandex_account, account, user):
    api, number, key = ready_order(yandex_account)
    scan(yandex_account, user, BARCODE)
    scan(yandex_account, user, code())
    finish_job(yandex_account, number)
    assert scan(yandex_account, user, number)['action'] == 'completed'
    marked_ozon(account)
    scan(account, user, BARCODE)
    rejected = scan(account, user, code())
    assert rejected['status'] == 'error'
    assert rejected['message'] == f'Данный Честный знак уже используется в заказе: {number}'
    saved = marking.order(yandex_account['id'], number)
    mark = marking.codes(saved, full=True)[0]
    assert mark['raw'] == code() and mark['status'] == 'not_recorded'
    assert marking_registry.binding(mark['fingerprint'])['state'] == 'bound'


def test_recorded_cis_must_keep_its_own_gtd(yandex_account, user):
    from tests.core.test_exemplar_data import GTD, enter
    api, number, key = ready_order(yandex_account, quantity=2)
    item = api._orders[number]['items'][0]
    item['requiredInstanceTypes'] = ['CIS', 'GTD']
    for value in (BARCODE, code(), BARCODE, code('ABCDEF1234568')):
        scan(yandex_account, user, value)
    other_gtd = GTD[:-1] + '8'
    enter(yandex_account, user, number, key, {'gtd': GTD}, unit=1)
    enter(yandex_account, user, number, key, {'gtd': other_gtd}, unit=2)
    item['instances'] = [{'cisFull': code(), 'gtd': other_gtd}, {'gtd': GTD}]
    finish_job(yandex_account, number)
    assert not packing.state(user)['marking']['local_only']
    assert scan(yandex_account, user, number)['status'] == 'error'
