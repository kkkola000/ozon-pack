"""A GTD-only order must reach the API that understands absence flags."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from app.core import db, marking, packing
from tests.core.test_exemplar_data import GTD, enter, require_ozon
from tests.core.test_gtd_omission import omit
from tests.core.test_marking import BARCODE, code, finish_job, scan


@pytest.mark.parametrize('status', ['awaiting_packaging', 'awaiting_deliver'])
@pytest.mark.parametrize('choice', ['omit', 'number'])
def test_gtd_only_order_skips_empty_mark_validation_and_requires_native_confirmation(account, user, monkeypatch, status, choice):
    api, number, key = require_ozon(account, status=status, cz=False)
    # Match an order whose absence has not yet been declared on Ozon.
    remote = api.exemplars_get(number)
    remote['products'][0]['exemplars'][0]['is_gtd_absent'] = False
    api._marking_payloads[number] = deepcopy(remote)
    scan(account, user, BARCODE)
    if choice == 'omit':
        omit(account, user, number, key)
    else:
        enter(account, user, number, key, {'gtd': GTD})

    def reject_empty(*_):
        return {'products': [{'product_id': int(key), 'valid': False, 'exemplars': [
            {'valid': False, 'marks': [], 'errors': ['Нет кодов маркировки для проверки']}]}]}

    check = Mock(side_effect=reject_empty)
    monkeypatch.setattr(api, 'exemplars_validate', check)
    marking.process(account, number, force=True)
    saved = marking.order(account['id'], number)
    assert saved['status'] == 'checking'
    assert saved['submitted_revision'] == saved['revision']
    assert not check.called
    assert len(api.marking_requests) == 1
    exemplar = api.marking_requests[0]['products'][0]['exemplars'][0]
    assert exemplar['is_gtd_absent'] is (choice == 'omit')
    assert exemplar.get('gtd') == (None if choice == 'omit' else GTD)
    assert not exemplar.get('marks')
    assert not packing.state(user)['complete']
    assert db.count('SELECT COUNT(*) FROM marking_codes') == 0
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'
    applied = api._postings[number]['product_exemplars']['products'][0]['exemplars'][0]
    assert applied['is_gtd_absent'] is (choice == 'omit')
    assert applied.get('gtd') == exemplar.get('gtd')
    assert scan(account, user, number)['action'] == 'completed'


def test_native_rejection_of_absence_still_blocks_completion(account, user, monkeypatch):
    api, number, key = require_ozon(account, cz=False)
    scan(account, user, BARCODE)
    omit(account, user, number, key)
    marking.process(account, number, force=True)
    original = api.exemplars_status

    def reject(order_id):
        data = original(order_id)
        data['products'][0]['exemplars'][0].update(gtd_check_status='ERROR', gtd_error_codes=['GTD_REQUIRED'])
        return data

    monkeypatch.setattr(api, 'exemplars_status', reject)
    marking.process(account, number, force=True)
    saved = marking.order(account['id'], number)
    assert saved['status'] == 'error'
    assert saved['submitted_revision'] == saved['revision']
    assert 'data:' + key + ':1:gtd' in saved['field_errors']
    assert packing.complete(user)['status'] == 'error'
    assert api._postings[number]['status'] == 'awaiting_packaging'


def test_mixed_gtd_number_and_absence_are_both_checked_after_set(account, user, monkeypatch):
    api, number, key = require_ozon(account, quantity=2, cz=False)
    scan(account, user, BARCODE)
    omit(account, user, number, key, unit=1)
    enter(account, user, number, key, {'gtd': GTD}, unit=2)
    monkeypatch.setattr(api, 'exemplars_validate', Mock(side_effect=AssertionError('No mark validation without marks')))
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'
    one, two = api.marking_requests[0]['products'][0]['exemplars']
    assert one['is_gtd_absent'] is True and 'gtd' not in one
    assert two['is_gtd_absent'] is False and two['gtd'] == GTD


def test_gtd_absence_does_not_skip_preflight_for_cz(account, user, monkeypatch):
    api, number, key = require_ozon(account)
    scan(account, user, BARCODE)
    scan(account, user, code())
    omit(account, user, number, key)
    original = api.exemplars_validate

    def reject(order_id, products):
        data = original(order_id, products)
        product = data['products'][0]
        product['valid'] = product['exemplars'][0]['valid'] = False
        product['exemplars'][0]['marks'][0]['valid'] = False
        return data

    check = Mock(side_effect=reject)
    monkeypatch.setattr(api, 'exemplars_validate', check)
    marking.process(account, number, force=True)
    saved = marking.order(account['id'], number)
    assert saved['status'] == 'error'
    assert saved['submitted_revision'] is None
    check.assert_called_once()
    assert not api.marking_requests
    assert marking.codes(saved)[0]['status'] == 'error'
    assert packing.complete(user)['status'] == 'error'
