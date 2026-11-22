"""A green M requires both the stored full CIS and its own successful check."""
from copy import deepcopy

import pytest

from app.core import db, exemplar_data, marking, marking_codes, packing
from app.markets.yandex import marking as yandex_marking
from tests.core.test_marking import BARCODE, code, marked_yandex, scan


def submitted(account, user, quantity=1):
    api, number, key = marked_yandex(account, quantity)
    for unit in range(quantity):
        scan(account, user, BARCODE)
        scan(account, user, code(f'ABCDEF123456{7 + unit}'))
    marking.process(account, number, force=True)
    saved = marking.order(account['id'], number)
    return api, number, key, exemplar_data.context(saved), marking.codes(saved, full=True)


def status_response(key, status='OK', value=None):
    return {'status': 'OK', 'result': {'items': [
        {'id': int(key), 'cis': [{'value': value or code(), 'status': status}]}]}}


@pytest.mark.parametrize('status', ['OK', 'INVALID', 'IN_PROGRESS', 'NOT_ON_VALIDATION', 'FAILED', 'UNKNOWN'])
def test_each_cis_status_controls_acceptance(yandex_account, user, monkeypatch, status):
    api, number, key, context, codes = submitted(yandex_account, user)
    monkeypatch.setattr(api, 'identifiers_status', lambda *_: status_response(key, status))
    marking.process(yandex_account, number, force=True)
    state = packing.state(user)
    expected = 'accepted' if status == 'OK' else 'error' if status == 'INVALID' else 'waiting'
    assert state['items'][0]['marking']['status'] == expected
    assert (marking.order(yandex_account['id'], number)['status'] == 'accepted') is (status == 'OK')
    assert api._orders[number]['substatus'] == ('READY_TO_SHIP' if status == 'OK' else 'STARTED')
    assert state['items'][0]['exemplar_data'] is None  # Ordinary CIS goods do not require a jewelry UIN.


@pytest.mark.parametrize('change', ['missing_full', 'short_only', 'different_crypto', 'other_item', 'other_campaign', 'program', 'cancelled'])
def test_successful_check_cannot_replace_exact_order_readback(yandex_account, user, monkeypatch, change):
    api, number, key, context, codes = submitted(yandex_account, user)
    raw = deepcopy(api._orders[number])
    if change == 'missing_full':
        raw['items'][0]['instances'] = []
    elif change == 'short_only':
        raw['items'][0]['instances'] = [{'cis': marking_codes.short(code())}]
    elif change == 'different_crypto':
        raw['items'][0]['instances'][0]['cisFull'] = code(tail='different-crypto')
    elif change == 'other_item':
        raw['items'][0]['id'] += 1
    elif change == 'other_campaign':
        raw['campaignId'] += 1
    elif change == 'program':
        raw['programType'] = 'DBS'
    else:
        raw['status'] = 'CANCELLED'
    monkeypatch.setattr(api, 'order_details', lambda *_: deepcopy(raw))
    monkeypatch.setattr(api, 'identifiers_status', lambda *_: status_response(key))
    if change in {'program', 'cancelled'}:
        with pytest.raises(ValueError):
            yandex_marking.poll(yandex_account, number, context, codes)
    else:
        assert yandex_marking.poll(yandex_account, number, context, codes)['status'] != 'accepted'


@pytest.mark.parametrize('response_type', ['outer_ok', 'outer_error', 'uin_only', 'wrong_code', 'wrong_item', 'duplicate_item'])
def test_status_must_belong_to_this_cis_and_item(yandex_account, user, monkeypatch, response_type):
    api, number, key, context, codes = submitted(yandex_account, user)
    data = status_response(key)
    item = data['result']['items'][0]
    if response_type == 'outer_ok':
        data = {'status': 'OK'}
    elif response_type == 'outer_error':
        data['status'] = 'ERROR'
    elif response_type == 'uin_only':
        item['uin'] = [{'value': marking_codes.short(code()), 'status': 'OK'}]
        del item['cis']
    elif response_type == 'wrong_code':
        item['cis'][0]['value'] = code('ABCDEF1234599')
    elif response_type == 'wrong_item':
        item['id'] += 1
    else:
        data['result']['items'].insert(0, {'id': int(key), 'cis': [{'value': code(), 'status': 'INVALID'}]})
    monkeypatch.setattr(api, 'identifiers_status', lambda *_: data)
    assert yandex_marking.poll(yandex_account, number, context, codes)['status'] != 'accepted'


@pytest.mark.parametrize('status', ['INVALID', 'IN_PROGRESS', 'NOT_ON_VALIDATION', 'FAILED'])
def test_final_confirmation_rechecks_cis_status(yandex_account, user, monkeypatch, status):
    api, number, key, context, codes = submitted(yandex_account, user)
    monkeypatch.setattr(api, 'identifiers_status', lambda *_: status_response(key, status))
    with pytest.raises(ValueError):
        yandex_marking.finalize(yandex_account, number, context, codes)


@pytest.mark.parametrize('status', ['INVALID', 'IN_PROGRESS'])
def test_status_change_during_ready_transition_does_not_turn_m_green(yandex_account, user, monkeypatch, status):
    api, number, key, context, codes = submitted(yandex_account, user)
    current = {'status': 'OK'}
    original = api.ready_to_ship

    def ready(*args):
        result = original(*args)
        current['status'] = status
        return result

    monkeypatch.setattr(api, 'ready_to_ship', ready)
    monkeypatch.setattr(api, 'identifiers_status', lambda *_: status_response(key, current['status']))
    marking.process(yandex_account, number, force=True)
    assert marking.order(yandex_account['id'], number)['status'] != 'accepted'
    assert packing.state(user)['items'][0]['marking']['status'] == ('error' if status == 'INVALID' else 'waiting')
    assert not db.query("SELECT id FROM events WHERE kind='marking_accepted'")


def test_all_units_need_their_own_ok(yandex_account, user, monkeypatch):
    api, number, key, context, codes = submitted(yandex_account, user, quantity=2)
    data = status_response(key)
    monkeypatch.setattr(api, 'identifiers_status', lambda *_: data)
    marking.process(yandex_account, number, force=True)
    assert packing.state(user)['items'][0]['marking']['status'] == 'waiting'
    data['result']['items'][0]['cis'].append({'value': code('ABCDEF1234568'), 'status': 'OK'})
    marking.process(yandex_account, number, force=True)
    assert marking.order(yandex_account['id'], number)['status'] == 'accepted'
    assert packing.state(user)['items'][0]['marking']['status'] == 'accepted'


@pytest.mark.parametrize('status', ['INVALID', 'IN_PROGRESS', 'NOT_ON_VALIDATION', 'FAILED'])
@pytest.mark.parametrize('during_finalization', [False, True])
def test_failed_completion_recheck_does_not_leave_a_stale_green_m(yandex_account, user, monkeypatch, status, during_finalization):
    api, number, key, context, codes = submitted(yandex_account, user)
    marking.process(yandex_account, number, force=True)
    assert packing.state(user)['items'][0]['marking']['status'] == 'accepted'
    calls = []

    def response(*_):
        calls.append(True)
        return status_response(key, 'OK' if during_finalization and len(calls) == 1 else status)

    monkeypatch.setattr(api, 'identifiers_status', response)
    assert scan(yandex_account, user, number)['status'] == 'error'
    assert packing.state(user)['items'][0]['marking']['status'] == ('error' if status == 'INVALID' else 'waiting')
    assert db.query_one('SELECT local_state FROM yandex_orders WHERE account_id=? AND id=?',
                        (yandex_account['id'], number))['local_state'] != 'packed'
