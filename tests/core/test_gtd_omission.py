"""Explicitly omitting GTD must survive writes, readback and cargo changes."""
from copy import deepcopy

import pytest

from app.core import db, exemplar_data, marking, packing
from app.core.config import settings
from app.markets.yandex.client import YandexError
from tests.core.test_exemplar_data import GTD, enter, require_ozon
from tests.core.test_marking import BARCODE, code, finish_job, marked_yandex, scan


def omit(account, user, number, key, *, unit=1, fields=None):
    saved = marking.order(account['id'], number)
    exemplar_data.save(account, user, number, {'revision': saved['revision'], 'items': [
        {'key': key, 'unit': unit, 'fields': {'gtd': '', **(fields or {})}, 'gtd_absent': True}]})


@pytest.mark.parametrize('status', ['awaiting_packaging', 'awaiting_deliver'])
@pytest.mark.parametrize('cz', [True, False])
def test_ozon_explicit_absence_is_sent_and_confirmed_without_losing_cz(account, user, status, cz):
    api, number, key = require_ozon(account, status=status, cz=cz)
    opened = scan(account, user, BARCODE)
    field = opened['state']['items'][0]['exemplar_data']['units'][0]['fields'][0]
    # The source defaults to absent; the operator must still choose explicitly.
    assert field['can_omit'] and not field['omitted']
    omit(account, user, number, key)
    saved = marking.order(account['id'], number)
    assert exemplar_data.values(saved) == {key: {'1': {'gtd': None}}}
    assert exemplar_data.public(saved, key)['units'][0]['fields'][0]['omitted']
    assert packing.complete(user)['status'] == 'error'
    if cz:
        assert saved['status'] == 'collecting'
        assert scan(account, user, code())['action'] == 'marking_saved'
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'
    sent = api.marking_requests[-1]['products'][0]['exemplars'][0]
    assert sent['is_gtd_absent'] is True and 'gtd' not in sent
    applied = api._postings[number]['product_exemplars']['products'][0]['exemplars'][0]
    assert applied['is_gtd_absent'] is True and 'gtd' not in applied
    if cz:
        assert sent['marks'] == [{'mark': code(), 'mark_type': 'mandatory_mark'}]
        assert packing.state(user)['items'][0]['marking']['status'] == 'accepted'
    assert scan(account, user, number)['action'] == 'completed'


@pytest.mark.parametrize('change', ['missing_flag', 'old_number', 'pending'])
def test_ozon_unconfirmed_absence_never_unblocks_assembly(account, user, monkeypatch, change):
    api, number, key = require_ozon(account, cz=False)
    scan(account, user, BARCODE)
    omit(account, user, number, key)
    marking.process(account, number, force=True)
    original = api.exemplars_status

    def stale(*args):
        data = original(*args)
        exemplar = data['products'][0]['exemplars'][0]
        if change == 'missing_flag':
            exemplar.pop('is_gtd_absent')
        elif change == 'old_number':
            exemplar['gtd'] = GTD
        else:
            exemplar['gtd_check_status'] = 'IN_PROGRESS'
        return data

    monkeypatch.setattr(api, 'exemplars_status', stale)
    marking.process(account, number, force=True)
    assert marking.order(account['id'], number)['status'] == 'checking'
    assert packing.complete(user)['status'] == 'error'
    assert api._postings[number]['status'] == 'awaiting_packaging'


def test_ozon_rejected_absence_can_be_replaced_with_number_without_rescanning_cz(account, user, monkeypatch):
    api, number, key = require_ozon(account)
    scan(account, user, BARCODE)
    scan(account, user, code())
    omit(account, user, number, key)
    marking.process(account, number, force=True)
    original = api.exemplars_status

    def rejected(*args):
        data = original(*args)
        data['products'][0]['exemplars'][0].update(gtd_check_status='ERROR', gtd_error_codes=['GTD_REQUIRED'])
        return data

    with monkeypatch.context() as patch:
        patch.setattr(api, 'exemplars_status', rejected)
        marking.process(account, number, force=True)
    saved = marking.order(account['id'], number)
    assert saved['status'] == 'error' and marking.codes(saved)[0]['status'] == 'saved'
    assert exemplar_data.public(saved, key)['units'][0]['fields'][0]['error']
    enter(account, user, number, key, {'gtd': GTD})
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'
    sent = api.marking_requests[-1]['products'][0]['exemplars'][0]
    assert sent['gtd'] == GTD and sent['is_gtd_absent'] is False
    assert sent['marks'][0]['mark'] == code()


@pytest.mark.parametrize('cz', [True, False])
def test_yandex_omission_removes_old_number_from_item_and_layout(yandex_account, user, cz):
    api, number, key = marked_yandex(yandex_account)
    raw = api._orders[number]
    raw['items'][0]['requiredInstanceTypes'] = ['GTD', *(['CIS'] if cz else [])]
    scan(yandex_account, user, BARCODE)
    # Another client filled a number after this panel opened its empty field.
    raw['items'][0]['instances'] = [{'gtd': GTD}]
    raw['delivery']['boxesLayout'] = [{'boxId': 10, 'items': [
        {'id': int(key), 'fullCount': 1, 'instances': [{'gtd': GTD}]}]}]
    omit(yandex_account, user, number, key)
    if cz:
        assert not getattr(api, 'box_requests', [])
        scan(yandex_account, user, code())
    finish_job(yandex_account, number)
    saved = marking.order(yandex_account['id'], number)
    assert saved['status'] == 'accepted', saved
    assert raw['substatus'] == 'READY_TO_SHIP'
    assert not any(i.get('gtd') for i in raw['items'][0]['instances'])
    for box in raw['delivery']['boxesLayout']:
        assert not any('gtd' in i for entry in box['items'] for i in entry.get('instances', []))
    if cz:
        assert raw['items'][0]['instances'][0]['cisFull'] == code()
    assert scan(yandex_account, user, number)['action'] == 'completed'


def test_yandex_omission_does_not_accept_a_stale_remote_number(yandex_account, user, monkeypatch):
    api, number, key = marked_yandex(yandex_account)
    raw = api._orders[number]
    raw['items'][0]['requiredInstanceTypes'] = ['GTD']
    scan(yandex_account, user, BARCODE)
    raw['items'][0]['instances'] = [{'gtd': GTD}]
    omit(yandex_account, user, number, key)
    monkeypatch.setattr(api, 'order_boxes', lambda *_: {'status': 'OK'})
    finish_job(yandex_account, number)
    assert marking.order(yandex_account['id'], number)['status'] == 'checking'
    assert raw['substatus'] == 'STARTED'
    assert packing.complete(user)['status'] == 'error'


@pytest.mark.parametrize('status', [400, None])
def test_yandex_readiness_refusal_allows_correction_but_timeout_stays_uncertain(yandex_account, user, monkeypatch, status):
    api, number, key = marked_yandex(yandex_account)
    raw = api._orders[number]
    raw['items'][0]['requiredInstanceTypes'] = ['GTD', 'CIS']
    scan(yandex_account, user, BARCODE)
    scan(yandex_account, user, code())
    omit(yandex_account, user, number, key)

    def refuse(*_):
        raise YandexError('sensitive external response', status=status)

    with monkeypatch.context() as patch:
        patch.setattr(api, 'ready_to_ship', refuse)
        finish_job(yandex_account, number)
    saved = marking.order(yandex_account['id'], number)
    assert saved['status'] == ('error' if status else 'checking')
    assert raw['substatus'] == 'STARTED'
    assert packing.complete(user)['status'] == 'error'
    assert 'sensitive' not in str(saved)
    if status:
        assert exemplar_data.public(saved, key)['units'][0]['fields'][0]['error']
        assert marking.codes(saved)[0]['status'] == 'saved'
        enter(yandex_account, user, number, key, {'gtd': GTD})
        finish_job(yandex_account, number)
        assert marking.order(yandex_account['id'], number)['status'] == 'accepted'
        assert raw['items'][0]['instances'][0]['gtd'] == GTD


@pytest.mark.parametrize('market', ['ozon', 'yandex'])
def test_mixed_number_and_omission_survive_automatic_split(account, yandex_account, user, monkeypatch, market):
    monkeypatch.setattr(settings, 'split_orders_on_pack', True)
    if market == 'ozon':
        shop = account
        api, number, key = require_ozon(shop, quantity=2, cz=False)
    else:
        shop = yandex_account
        api, number, key = marked_yandex(shop, quantity=2)
        api._orders[number]['items'][0]['requiredInstanceTypes'] = ['GTD']
    scan(shop, user, BARCODE)
    omit(shop, user, number, key, unit=1)
    assert marking.order(shop['id'], number)['status'] == 'collecting'
    enter(shop, user, number, key, {'gtd': GTD}, unit=2)
    finish_job(shop, number)
    saved = marking.order(shop['id'], number)
    assert saved['status'] == 'accepted', saved
    if market == 'ozon':
        rows = db.query('SELECT child_id,marks_verified FROM cargo_split_children WHERE account_id=?', (shop['id'],))
        assert len(rows) == 2 and all(r['marks_verified'] for r in rows)
        values = [exemplar_data.values(marking.order(shop['id'], r['child_id']))[key]['1']['gtd'] for r in rows]
        assert None in values and GTD in values
    else:
        boxes = api._orders[number]['delivery']['boxesLayout']
        assert len(boxes) == 2
        values = [b['items'][0].get('instances', [{}])[0].get('gtd') for b in boxes]
        assert values == [None, GTD]


def test_yandex_empty_instances_can_be_omitted_in_mixed_units_readback(yandex_account, user):
    from app.markets.yandex.marking import _saved_data
    api, number, key = marked_yandex(yandex_account, quantity=2)
    raw = deepcopy(api._orders[number])
    raw['items'][0]['instances'] = [{'gtd': GTD}]
    context = {'_panel_details': {key: {'1': {'gtd': None}, '2': {'gtd': GTD}}}}
    assert _saved_data(raw, context, [])
    raw['items'][0]['instances'].append({'gtd': 'unexpected'})
    assert not _saved_data(raw, context, [])


@pytest.mark.parametrize('flag, value', [(True, GTD), (False, ''), (False, None), ('true', ''), (1, ''), (None, '')])
def test_absence_requires_explicit_boolean_and_no_number(account, user, flag, value):
    _, number, key = require_ozon(account, cz=False)
    scan(account, user, BARCODE)
    saved = marking.order(account['id'], number)
    with pytest.raises(ValueError):
        exemplar_data.save(account, user, number, {'revision': saved['revision'], 'items': [
            {'key': key, 'unit': 1, 'fields': {'gtd': value}, 'gtd_absent': flag}]})
    assert marking.order(account['id'], number)['revision'] == saved['revision']


def test_omission_does_not_bypass_other_required_data(account, user):
    _, number, key = require_ozon(account, fields=('gtd', 'rnpt'), cz=False)
    scan(account, user, BARCODE)
    with pytest.raises(ValueError):
        omit(account, user, number, key, fields={'rnpt': ''})
    omit(account, user, number, key, fields={'rnpt': '12345678901234'})
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'


def test_omission_flag_cannot_bypass_a_different_required_field(account, user):
    _, number, key = require_ozon(account, fields=('rnpt',), cz=False)
    scan(account, user, BARCODE)
    saved = marking.order(account['id'], number)
    with pytest.raises(ValueError):
        exemplar_data.save(account, user, number, {'revision': saved['revision'], 'items': [
            {'key': key, 'unit': 1, 'fields': {'rnpt': ''}, 'gtd_absent': True}]})
