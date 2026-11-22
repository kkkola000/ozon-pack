"""Removed jewelry fields must not block ordinary goods or revive old UIN jobs."""
import json
from unittest.mock import Mock

import pytest

from app.core import db, exemplar_data, marking, packing
from tests.core.test_exemplar_data import GTD, require_ozon
from tests.core.test_marking import BARCODE, code, finish_job, marked_yandex, scan


def test_ozon_uin_only_requirement_does_not_open_a_field_or_block_packing(account, user):
    api, number, sku = require_ozon(account, fields=('uin',), cz=False)
    result = scan(account, user, BARCODE)
    assert result['status'] == 'ok'
    assert result['state']['complete']
    assert not result['state']['items'][0].get('exemplar_data')
    assert scan(account, user, number)['action'] == 'completed'
    assert not api.marking_requests


@pytest.mark.parametrize('market', ['ozon', 'yandex'])
def test_legacy_uin_data_does_not_block_cis_or_gtd_or_get_submitted(account, yandex_account, user, market):
    if market == 'ozon':
        shop = account
        api, number, key = require_ozon(account, fields=('gtd', 'uin'))
    else:
        shop = yandex_account
        api, number, key = marked_yandex(shop)
        api._orders[number]['items'][0]['requiredInstanceTypes'] = ['CIS', 'GTD', 'UIN']
    scan(shop, user, BARCODE)
    saved = marking.order(shop['id'], number)
    products = json.loads(saved['products'])
    products[0]['fields'].append('uin')
    old_details = {key: {'1': {'gtd': GTD, 'uin': '1234567890123456'}}}
    encrypted = marking._seal(json.dumps(old_details))
    db.execute('UPDATE marking_orders SET products=?,details=? WHERE id=?',
               (json.dumps(products), encrypted, saved['id']))
    saved = marking.order(shop['id'], number)
    public = exemplar_data.public(saved, key)
    assert [field['name'] for field in public['units'][0]['fields']] == ['gtd']
    assert exemplar_data.values(saved) == {key: {'1': {'gtd': GTD}}}
    assert exemplar_data.context(saved)['_panel_details'] == {key: {'1': {'gtd': GTD}}}
    scan(shop, user, code())
    finish_job(shop, number)
    saved = marking.order(shop['id'], number)
    assert saved['status'] == 'accepted'
    assert saved['details'] == encrypted  # Historic encrypted values are not destroyed.
    if market == 'ozon':
        sent = api.marking_requests[-1]['products'][0]['exemplars'][0]
        assert sent['gtd'] == GTD
        assert {m['mark_type'] for m in sent['marks']} == {'mandatory_mark'}
    else:
        sent = api.box_requests[-1][0]['items'][0]['instances'][0]
        assert sent['gtd'] == GTD and sent['cis'] == code() and 'uin' not in sent
    assert scan(shop, user, number)['action'] == 'completed'


@pytest.mark.parametrize('status', ['queued', 'checking', 'uncertain'])
def test_obsolete_uin_only_job_stops_without_writes_or_false_acceptance(yandex_account, user, monkeypatch, status):
    api, number, key = marked_yandex(yandex_account)
    api._orders[number]['items'][0]['requiredInstanceTypes'] = ['UIN']
    scan(yandex_account, user, BARCODE)
    saved = marking.order(yandex_account['id'], number)
    products = json.loads(saved['products'])
    products[0]['fields'] = ['uin']
    encrypted = marking._seal(json.dumps({key: {'1': {'uin': '1234567890123456'}}}))
    db.execute('UPDATE marking_orders SET products=?,details=?,status=? WHERE id=?',
               (json.dumps(products), encrypted, status, saved['id']))
    read = Mock(side_effect=AssertionError('An obsolete UIN task should not call the marketplace'))
    monkeypatch.setattr(api, 'order_details', read)
    marking.process(yandex_account, number, force=True)
    read.assert_not_called()
    saved = marking.order(yandex_account['id'], number)
    assert saved['status'] == 'collecting' and saved['details'] == encrypted
    assert not exemplar_data.required(saved) and not exemplar_data.values(saved)
    assert not packing.state(user)['marking']['poll']
    assert not db.query("SELECT id FROM events WHERE kind='marking_accepted'")


@pytest.mark.parametrize('status', ['ERROR', 'FAILED'])
def test_ozon_uin_check_does_not_override_cis_result(account, user, monkeypatch, status):
    api, number, key = require_ozon(account, fields=())
    scan(account, user, BARCODE)
    scan(account, user, code())
    original = api.exemplars_status

    def result(order_id):
        data = original(order_id)
        data['products'][0]['exemplars'][0]['marks'].append(
            {'mark_type': 'jw_uin', 'mark': '1234567890123456', 'check_status': status, 'error_codes': ['UIN_INVALID']})
        return data

    monkeypatch.setattr(api, 'exemplars_status', result)
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'
    assert packing.state(user)['items'][0]['marking']['status'] == 'accepted'
