"""Диагностика читает требования обоих статусов без отправки кодов и утечки данных."""
import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
import httpx

from app.core import db, marking
from app import order_requirements_diagnostics as diagnostic
from app.markets.ozon.client import OzonError
from app.markets.yandex.client import YandexError
from tests.core.test_marking import BARCODE, code, marked_ozon, marked_yandex, scan
from tests.markets.ozon.test_ready_marking import ordinary_order


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    monkeypatch.setattr(httpx.Client, 'send', Mock(side_effect=AssertionError('Network is forbidden in this test')))


@pytest.mark.parametrize('status', ['awaiting_packaging', 'awaiting_deliver'])
def test_ozon_reads_exemplars_without_legacy_hints(account, monkeypatch, status):
    api, number, sku = ordinary_order(account, status=status)
    original = api.exemplars_get

    def modern(order_id):
        result = original(order_id)
        result['products'][0].update(is_mandatory_mark_needed=True, is_mandatory_mark_possible=True, is_gtd_needed=True)
        return result

    read = Mock(side_effect=modern)
    monkeypatch.setattr(api, 'exemplars_get', read)
    monkeypatch.setattr(api, 'ship', Mock(side_effect=AssertionError('must not ship')))
    monkeypatch.setattr(api, 'exemplars_set', Mock(side_effect=AssertionError('must not set')))
    before = [dict(r) for r in db.query('SELECT * FROM postings')]
    result = diagnostic.inspect_order(account, number)
    product = result['exemplars']['products'][0]
    assert result['remote_status'] == status
    assert result['posting_requirements']['products_requiring_mandatory_mark'] == []
    assert product['product_id'] == sku and product['is_mandatory_mark_needed'] is True
    assert product['is_mandatory_mark_possible'] is True and product['is_gtd_needed'] is True
    assert [dict(r) for r in db.query('SELECT * FROM postings')] == before
    assert marking.order(account['id'], number) is None
    read.assert_called_once_with(number)
    assert not api.ship.called and not api.exemplars_set.called


def test_ozon_report_excludes_codes_declarations_and_credentials(account, monkeypatch):
    api, number, _sku = marked_ozon(account)
    raw = api._postings[number]
    raw['api_key'] = account['api_key'] = 'secret-production-key'
    raw['customer'] = {'name': 'private-buyer', 'phone': 'private-phone'}
    data = api.exemplars_get(number)
    data['products'][0]['exemplars'][0].update(gtd='secret-gtd', rnpt='secret-rnpt',
                                             is_gtd_absent=False, marks=[{'mark_type': 'mandatory_mark', 'mark': code()}])
    monkeypatch.setattr(api, 'exemplars_get', Mock(return_value=data))
    result = diagnostic.inspect_order(account, number)
    encoded = json.dumps(result)
    for secret in ('secret-production-key', 'private-buyer', 'private-phone', 'secret-gtd', 'secret-rnpt', code()):
        assert secret not in encoded
    product = result['exemplars']['products'][0]
    assert product['mark_types_present'] == {'mandatory_mark': 1}
    assert product['gtd_filled_count'] == product['rnpt_filled_count'] == 1


def test_ozon_error_does_not_expose_raw_api_message(account, monkeypatch):
    api, number, _sku = ordinary_order(account)
    monkeypatch.setattr(api, 'exemplars_get', Mock(side_effect=OzonError('token-and-full-code-secret', status=403)))
    result = diagnostic.inspect_order(account, number)
    assert result['exemplars']['error'] == {'type': 'OzonError', 'http_status': 403}
    assert 'token-and-full-code-secret' not in json.dumps(result)


@pytest.mark.parametrize('changed', [False, True])
def test_ozon_diagnostic_distinguishes_empty_mark_preflight_and_changed_ids(account, user, monkeypatch, changed):
    from tests.core.test_exemplar_data import require_ozon
    from tests.core.test_gtd_omission import omit
    api, number, key = require_ozon(account, cz=False)
    scan(account, user, BARCODE)
    omit(account, user, number, key)
    original = api.exemplars_get

    def read(order_id):
        data = original(order_id)
        if changed:
            data['products'][0]['exemplars'][0]['exemplar_id'] += 1
        return data

    monkeypatch.setattr(api, 'exemplars_get', read)
    for method in ('exemplars_validate', 'exemplars_set', 'exemplars_update', 'ship'):
        monkeypatch.setattr(api, method, Mock(side_effect=AssertionError('Diagnostics must not write or validate')))
    before = [dict(r) for r in db.query('SELECT * FROM marking_orders')]
    result = diagnostic.inspect_order(account, number)
    assert result['submission'] == {
        'current_revision_dispatched': False, 'exemplar_ids_match_snapshot': not changed,
        'mark_prevalidation_needed': False,
        'gtd_choices': [{'product_id': key, 'unit': 1, 'choice': 'omit'}]}
    assert [dict(r) for r in db.query('SELECT * FROM marking_orders')] == before


def test_ozon_submission_report_does_not_expose_gtd_or_cz(account, user):
    from tests.core.test_exemplar_data import GTD, enter, require_ozon
    _, number, key = require_ozon(account)
    scan(account, user, BARCODE)
    scan(account, user, code())
    enter(account, user, number, key, {'gtd': GTD})
    result = diagnostic.inspect_order(account, number)
    assert result['submission']['mark_prevalidation_needed'] is True
    assert result['submission']['gtd_choices'] == [{'product_id': key, 'unit': 1, 'choice': 'number'}]
    assert code() not in json.dumps(result) and GTD not in json.dumps(result)


@pytest.mark.parametrize('substatus', ['STARTED', 'READY_TO_SHIP'])
def test_yandex_reports_cis_optional_and_gtd_in_both_stages(yandex_account, monkeypatch, substatus):
    api, number, _key = marked_yandex(yandex_account)
    raw = api._orders[number]
    raw['substatus'] = substatus
    raw['items'][0]['requiredInstanceTypes'] = ['CIS_OPTIONAL', 'GTD', 'RNPT']
    raw['items'][0]['instances'] = [{'cisFull': code(), 'cis': 'secret-short-code', 'gtd': 'secret-gtd', 'rnpt': 'secret-rnpt'}]
    before = deepcopy(raw)
    monkeypatch.setattr(api, 'order_boxes', Mock(side_effect=AssertionError('must not write boxes')))
    monkeypatch.setattr(api, 'ready_to_ship', Mock(side_effect=AssertionError('must not change status')))
    result = diagnostic.inspect_order(yandex_account, number)
    assert result['remote_substatus'] == substatus
    assert result['products'][0]['requiredInstanceTypes'] == ['CIS_OPTIONAL', 'GTD', 'RNPT']
    assert result['products'][0]['cis_full_present_count'] == 1
    for secret in (code(), 'secret-short-code', 'secret-gtd', 'secret-rnpt'):
        assert secret not in json.dumps(result)
    assert api._orders[number] == before
    assert not api.order_boxes.called and not api.ready_to_ship.called


def test_report_includes_saved_required_and_possible_separately(account, user):
    _api, number, _sku = marked_ozon(account)
    assert scan(account, user, BARCODE)['status'] == 'ok'
    result = diagnostic.inspect_order(account, number)
    assert result['local']['marking_snapshot_present']
    assert result['local']['products'][0]['required'] is True
    assert result['local']['products'][0]['possible'] is True
    assert result['local']['products'][0]['stored_code_count'] == 0


def test_main_uses_configured_account_and_prints_safe_json(account, capsys):
    _api, number, _sku = ordinary_order(account)
    assert diagnostic.main(['--market', 'ozon', '--order', number]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['account_id'] == account['id'] and result['order_id'] == number
    assert result['exemplars']['posting_number_matches'] is True


def test_account_ambiguity_requires_explicit_id(account, monkeypatch):
    duplicate = {**account, 'id': -10}
    monkeypatch.setattr(diagnostic.accounts, 'all_accounts', lambda **_: [account, duplicate])
    with pytest.raises(ValueError, match='AccountNotConfirmed'):
        diagnostic._account('ozon', 'missing-order', None)
    assert diagnostic._account('ozon', 'missing-order', account['id'])['id'] == account['id']


def test_yandex_reports_check_even_without_remote_instances(yandex_account, user, monkeypatch):
    api, number, key = marked_yandex(yandex_account)
    scan(yandex_account, user, BARCODE)
    scan(yandex_account, user, code())
    data = {'status': 'OK', 'secret-key': 'private-key', 'result': {'items': [
        {'id': int(key), 'cis': [{'value': code(), 'status': 'INVALID', 'substatus': 'INVALID_CRYPTO_TAIL'}]}]}}
    monkeypatch.setattr(api, 'identifiers_status', Mock(return_value=data))
    before = [dict(r) for r in db.query('SELECT * FROM marking_codes')]
    result = diagnostic.inspect_order(yandex_account, number)
    assert result['products'][0]['instance_count'] == 0
    assert result['readback']['all_full_codes_saved'] is False
    assert result['readback']['codes'][0]['full_code_saved'] is False
    check = result['identifiers_status']['items'][0]['cis'][0]
    assert check == {'status': 'INVALID', 'substatus': 'INVALID_CRYPTO_TAIL', 'value_present': True,
                     'matching_local_units': [1], 'full_value_matches_local': True}
    assert result['identifiers_status']['panel_code_check'] == 'error'
    assert [dict(r) for r in db.query('SELECT * FROM marking_codes')] == before
    assert result['local']['attempts'] == 0
    assert result['local']['worker_enabled'] is False
    encoded = json.dumps(result)
    assert code() not in encoded and 'private-key' not in encoded
    assert 'ABCDEF1234567' not in encoded


def test_yandex_diagnostic_separates_check_ok_from_full_code_readback(yandex_account, user, monkeypatch):
    api, number, key = marked_yandex(yandex_account)
    scan(yandex_account, user, BARCODE)
    scan(yandex_account, user, code())
    monkeypatch.setattr(api, 'identifiers_status', Mock(return_value={'status': 'OK', 'result': {'items': [
        {'id': int(key), 'cis': [{'value': code(), 'status': 'OK'}]}]}}))
    result = diagnostic.inspect_order(yandex_account, number)
    assert result['identifiers_status']['panel_code_check'] == 'accepted'
    assert result['readback']['all_full_codes_saved'] is False
    assert marking.order(yandex_account['id'], number)['status'] == 'queued'


def test_yandex_check_permission_error_is_reported_without_losing_order_details(yandex_account, monkeypatch):
    api, number, _ = marked_yandex(yandex_account)
    monkeypatch.setattr(api, 'identifiers_status', Mock(side_effect=YandexError('private-token-and-full-code', status=403)))
    result = diagnostic.inspect_order(yandex_account, number)
    assert result['remote_status'] == 'PROCESSING'
    assert result['identifiers_status']['error'] == {'type': 'YandexError', 'http_status': 403}
    assert 'private-token-and-full-code' not in json.dumps(result)
