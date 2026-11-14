"""Требование ЧЗ из экземпляров Ozon при пустом прежнем списке requirements."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from app.core import db, marking, packing
from app.markets.ozon import store
from app.markets.ozon.client import OzonError
from tests.core.test_marking import BARCODE, code, finish_job, scan
from tests.markets.ozon.test_ready_marking import ordinary_order


def required_in_exemplars(account, monkeypatch, *, status='awaiting_deliver', action='update_cis'):
    api, number, sku = ordinary_order(account, status=status)
    api._postings[number]['available_actions'] = [action, 'cancel']
    original = api.exemplars_get

    def modern(order_id):
        data = original(order_id)
        for product in data['products']:
            if str(product['product_id']) == sku:
                product.update(is_mandatory_mark_needed=True, is_mandatory_mark_possible=True)
        return data

    calls = Mock(side_effect=modern)
    monkeypatch.setattr(api, 'exemplars_get', calls)
    return api, number, sku, calls


@pytest.mark.parametrize('status', ['awaiting_packaging', 'awaiting_deliver'])
@pytest.mark.parametrize('action', ['ship_with_additional_info', 'update_cis'])
def test_additional_info_discovers_required_mark_and_transmits_full_code(account, user, monkeypatch, status, action):
    api, number, sku, calls = required_in_exemplars(account, monkeypatch, status=status, action=action)
    ship = Mock(wraps=api.ship)
    monkeypatch.setattr(api, 'ship', ship)
    opened = scan(account, user, BARCODE)
    assert opened['status'] == 'ok', opened
    assert opened['state']['items'][0].get('marking', {}).get('status') == 'waiting'
    assert opened['state']['marking']['pending_key'] == sku
    assert not opened['state']['complete']
    assert calls.called
    assert packing.complete(user)['status'] == 'error'
    assert scan(account, user, number)['status'] == 'error'
    assert not ship.called
    saved = scan(account, user, code())
    assert saved['action'] == 'marking_saved', saved
    finish_job(account, number)
    state = packing.state(user)
    assert state['items'][0]['marking']['status'] == 'accepted', state
    assert state['complete']
    assert api.marking_requests[0]['products'][0]['exemplars'][0]['marks'][0]['mark'] == code()
    assert marking.codes(marking.order(account['id'], number), full=True)[0]['raw'] == code()
    assert ship.call_count == (1 if status == 'awaiting_packaging' else 0)
    assert scan(account, user, number)['action'] == 'completed'


@pytest.mark.parametrize('response', ['empty', 'forbidden', 'timeout'])
def test_unverified_additional_info_cannot_be_treated_as_unmarked(account, user, monkeypatch, response):
    api, number, _sku, _calls = required_in_exemplars(account, monkeypatch)
    unavailable = Mock(return_value={}) if response == 'empty' else Mock(side_effect=OzonError(
        'unavailable', status=403 if response == 'forbidden' else None))
    monkeypatch.setattr(api, 'exemplars_get', unavailable)
    result = scan(account, user, BARCODE)
    assert result['status'] == 'error', result
    assert not result['state']['active']
    assert db.query_one('SELECT local_state FROM postings WHERE account_id=? AND posting_number=?',
                        (account['id'], number))['local_state'] != 'packed'
    assert not db.query('SELECT * FROM marking_codes')


def test_non_marking_additional_info_does_not_show_m_or_force_chz(account, user, monkeypatch):
    api, number, _sku = ordinary_order(account)
    api._postings[number]['available_actions'] = ['update_cis']
    original = api.exemplars_get

    def customs_only(order_id):
        data = original(order_id)
        data['products'][0].update(is_gtd_needed=True, is_mandatory_mark_needed=False, is_mandatory_mark_possible=False)
        return data

    monkeypatch.setattr(api, 'exemplars_get', customs_only)
    result = scan(account, user, BARCODE)
    assert result['status'] == 'ok', result
    assert not result['state']['items'][0].get('marking')
    assert not result['state']['complete']
    assert result['state']['items'][0]['exemplar_data']['units'][0]['fields'][0]['name'] == 'gtd'
    assert marking.order(account['id'], number)['status'] == 'collecting'
    assert scan(account, user, number)['status'] == 'error'


@pytest.mark.parametrize('requirement', ['products_requiring_gtd', 'products_requiring_rnpt', 'products_requiring_weight'])
def test_additional_requirement_also_requests_modern_marking_flags(account, user, monkeypatch, requirement):
    api, number, sku, _calls = required_in_exemplars(account, monkeypatch, action='ship')
    api._postings[number]['requirements'][requirement] = [sku]
    result = scan(account, user, BARCODE)
    assert result['status'] == 'ok', result
    assert result['state']['items'][0].get('marking', {}).get('status') == 'waiting'
    assert not result['state']['complete']


def test_detected_requirement_survives_sync_and_disappearing_actions(account, user, monkeypatch):
    api, number, sku, _calls = required_in_exemplars(account, monkeypatch)
    assert scan(account, user, BARCODE)['status'] == 'ok'
    assert marking.has_required(marking.order(account['id'], number))
    assert db.query_one('SELECT mandatory_mark FROM posting_items WHERE account_id=? AND posting_number=? AND sku=?',
                        (account['id'], number, sku))['mandatory_mark'] == 1
    api._postings[number]['available_actions'] = ['ship']
    with db.write() as conn:
        store.upsert_posting(conn, account['id'], api._postings[number])
    monkeypatch.setattr(api, 'exemplars_get', Mock(side_effect=OzonError('temporary', status=500)))
    assert packing.complete(user)['status'] == 'error'
    assert db.query_one('SELECT local_state FROM postings WHERE account_id=? AND posting_number=?',
                        (account['id'], number))['local_state'] != 'packed'
    assert packing.state(user)['items'][0]['marking']['status'] != 'accepted'


def test_order_already_open_without_m_is_blocked_when_requirement_appears(account, user, monkeypatch):
    api, number, _sku = ordinary_order(account)
    opened = scan(account, user, BARCODE)
    assert opened['state']['complete'] and not opened['state']['items'][0].get('marking')
    api._postings[number]['available_actions'] = ['update_cis']
    original = api.exemplars_get

    def required(order_id):
        data = original(order_id)
        data['products'][0].update(is_mandatory_mark_needed=True, is_mandatory_mark_possible=True)
        return data

    monkeypatch.setattr(api, 'exemplars_get', required)
    result = packing.complete(user)
    assert result['status'] == 'error', result
    assert not result['state']['complete']
    assert result['state']['items'][0]['marking']['status'] == 'waiting'


def test_only_required_product_gets_m_in_a_mixed_order(account, user, monkeypatch):
    api, number, sku, _calls = required_in_exemplars(account, monkeypatch)
    plain = deepcopy(api._postings[number]['products'][0])
    plain.update(sku=int(sku)+1, offer_id='plain-product', name='Обычный товар')
    api._postings[number]['products'].append(plain)
    with db.write() as conn:
        store.upsert_posting(conn, account['id'], api._postings[number])
    result = scan(account, user, BARCODE)
    assert result['status'] == 'ok', result
    items = {i['sku']: i for i in result['state']['items']}
    assert items[sku].get('marking', {}).get('status') == 'waiting'
    assert not items[str(plain['sku'])].get('marking')


def test_existing_remote_mark_is_not_hidden_by_cleared_needed_flag(account, user):
    api, number, _sku = ordinary_order(account)
    api._postings[number]['available_actions'] = ['update_cis']
    data = api.exemplars_get(number)
    data['products'][0]['exemplars'][0]['marks'] = [{'mark_type': 'mandatory_mark', 'mark': code()}]
    api._marking_payloads[number] = deepcopy(data)
    result = scan(account, user, BARCODE)
    assert result['status'] == 'ok', result
    assert result['state']['items'][0]['marking']['status'] == 'waiting'
    assert not result['state']['complete']
    assert not marking.codes(marking.order(account['id'], number))
    assert scan(account, user, code())['action'] == 'marking_saved'
    finish_job(account, number)
    assert packing.state(user)['items'][0]['marking']['status'] == 'accepted'
