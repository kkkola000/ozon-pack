"""Ozon GTD writes preserve the full exemplar set and never bypass readback."""
from copy import deepcopy

import pytest

from app.core import marking, packing
from app.markets.ozon import marking as ozon_marking
from tests.core.test_exemplar_data import GTD, enter, require_ozon
from tests.core.test_gtd_omission import omit
from tests.core.test_marking import BARCODE, code, finish_job, scan


def test_gtd_write_preserves_fresh_remote_cz_and_other_exemplars(account, monkeypatch):
    api, number, key = require_ozon(account, quantity=2)
    fresh = api.exemplars_get(number)
    fresh['multi_box_qty'] = 2
    first, second = fresh['products'][0]['exemplars']
    first.update(gtd=GTD, is_gtd_absent=False, rnpt='12345678901234', is_rnpt_absent=False,
                 marks=[{'mark': code(), 'mark_type': 'mandatory_mark'}])
    second.update(marks=[{'mark': code('ABCDEF1234568'), 'mark_type': 'mandatory_mark'}], weight=1500)
    extra = {'product_id': 987654, 'quantity': 1, 'exemplars': [
        {'exemplar_id': 987654001, 'gtd': GTD, 'is_gtd_absent': False,
         'marks': [{'mark': code('ABCDEF1234569'), 'mark_type': 'mandatory_mark'}]}]}
    fresh['products'].append(extra)
    context = deepcopy(fresh)
    context['_panel_details'] = {key: {'1': {'gtd': None}, '2': {'gtd': GTD + '8'}}}
    # A remote field changed after the panel opened: keep the freshest value.
    first['rnpt'] = '23456789012345'
    validations, writes = [], []
    original = api.exemplars_validate

    def validate(order_id, products):
        validations.append(deepcopy(products))
        return original(order_id, products)

    monkeypatch.setattr(api, 'exemplars_get', lambda _: deepcopy(fresh))
    monkeypatch.setattr(api, 'exemplars_validate', validate)
    ozon_marking.submit(account, number, context, [], lambda: writes.append(True))
    assert writes == [True]
    sent = api.marking_requests[-1]
    assert sent['multi_box_qty'] == 2
    assert len(sent['products']) == 2
    one, two = sent['products'][0]['exemplars']
    assert one['is_gtd_absent'] is True and 'gtd' not in one
    assert two['gtd'] == GTD + '8' and two['is_gtd_absent'] is False
    assert one['rnpt'] == first['rnpt'] and one['is_rnpt_absent'] is False
    assert two['weight'] == 1500
    assert [e['marks'] for e in (one, two)] == [first['marks'], second['marks']]
    assert sent['products'][1]['exemplars'] == extra['exemplars']
    assert [e['exemplar_id'] for e in (one, two)] == [first['exemplar_id'], second['exemplar_id']]
    for product in validations[0]:
        for exemplar in product['exemplars']:
            assert not {'exemplar_id', 'is_gtd_absent', 'is_rnpt_absent'} & exemplar.keys()
    assert 'gtd' not in validations[0][0]['exemplars'][0]


@pytest.mark.parametrize('omitted', [False, True])
def test_failed_gtd_check_polls_without_resending_already_saved_cz(account, user, monkeypatch, omitted):
    api, number, key = require_ozon(account)
    scan(account, user, BARCODE)
    scan(account, user, code())
    if omitted:
        omit(account, user, number, key)
    else:
        enter(account, user, number, key, {'gtd': GTD})
    marking.process(account, number, force=True)
    original = api.exemplars_status

    def failed(order_id):
        data = original(order_id)
        data['products'][0]['exemplars'][0]['gtd_check_status'] = 'FAILED'
        return data

    with monkeypatch.context() as patch:
        patch.setattr(api, 'exemplars_status', failed)
        for _ in range(3):
            marking.process(account, number, force=True)
        saved = marking.order(account['id'], number)
        assert saved['status'] == 'checking'
        assert packing.complete(user)['status'] == 'error'
        assert len(api.marking_requests) == 1
        assert marking.codes(saved, full=True)[0]['raw'] == code()
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'
    assert len(api.marking_requests) == 1
    assert scan(account, user, number)['action'] == 'completed'


@pytest.mark.parametrize('bad_response', ['missing_unit', 'duplicate_product'])
def test_incomplete_gtd_validation_does_not_dispatch_any_write(account, user, monkeypatch, bad_response):
    api, number, key = require_ozon(account, quantity=2)
    for serial in ('ABCDEF1234567', 'ABCDEF1234568'):
        scan(account, user, BARCODE)
        scan(account, user, code(serial))
    omit(account, user, number, key, unit=1)
    enter(account, user, number, key, {'gtd': GTD}, unit=2)
    original = api.exemplars_validate

    def partial(order_id, products):
        data = original(order_id, products)
        if bad_response == 'missing_unit':
            data['products'][0]['exemplars'].pop()
        else:
            data['products'].append(deepcopy(data['products'][0]))
        return data

    with monkeypatch.context() as patch:
        patch.setattr(api, 'exemplars_validate', partial)
        marking.process(account, number, force=True)
    saved = marking.order(account['id'], number)
    assert saved['submitted_revision'] is None
    assert not api.marking_requests
    assert packing.complete(user)['status'] == 'error'
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'


@pytest.mark.parametrize('bad_response', ['duplicate_unit', 'extra_product'])
def test_ambiguous_gtd_status_never_confirms_readiness(account, user, monkeypatch, bad_response):
    api, number, key = require_ozon(account, cz=False)
    scan(account, user, BARCODE)
    omit(account, user, number, key)
    marking.process(account, number, force=True)
    original = api.exemplars_status

    def ambiguous(order_id):
        data = original(order_id)
        if bad_response == 'duplicate_unit':
            data['products'][0]['exemplars'].append(deepcopy(data['products'][0]['exemplars'][0]))
        else:
            product = deepcopy(data['products'][0])
            product['product_id'] = 987654
            data['products'].append(product)
        return data

    with monkeypatch.context() as patch:
        patch.setattr(api, 'exemplars_status', ambiguous)
        marking.process(account, number, force=True)
        assert marking.order(account['id'], number)['status'] == 'checking'
        assert api._postings[number]['status'] == 'awaiting_packaging'
        assert packing.complete(user)['status'] == 'error'
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'


def test_omitting_gtd_never_bypasses_a_rejected_cz(account, user, monkeypatch):
    api, number, key = require_ozon(account)
    scan(account, user, BARCODE)
    scan(account, user, code())
    omit(account, user, number, key)
    marking.process(account, number, force=True)
    original = api.exemplars_status

    def rejected(order_id):
        data = original(order_id)
        data['products'][0]['exemplars'][0]['marks'][0].update(check_status='ERROR', error_codes=['MARK_INVALID'])
        return data

    monkeypatch.setattr(api, 'exemplars_status', rejected)
    marking.process(account, number, force=True)
    saved = marking.order(account['id'], number)
    assert saved['status'] == 'error'
    assert marking.codes(saved)[0]['status'] == 'error'
    assert api._postings[number]['status'] == 'awaiting_packaging'
    assert packing.complete(user)['status'] == 'error'
