"""Готовый заказ без ЧЗ не должен зависеть от методов работы с экземплярами."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from app.core import db, marking, order_lock, packing
from app.markets.ozon import store
from app.markets.ozon.client import OzonError
from tests.core.test_marking import BARCODE, code, finish_job, marked_ozon, scan


def ordinary_order(account, status='awaiting_deliver'):
    api, number, sku = marked_ozon(account, status=status)
    api._postings[number]['requirements']['products_requiring_mandatory_mark'] = []
    with db.write() as conn:
        store.upsert_posting(conn, account['id'], api._postings[number])
    return api, number, sku


@pytest.mark.parametrize('via', ['product', 'label'])
@pytest.mark.parametrize('response', ['refused', 'empty'])
def test_ready_ordinary_order_opens_and_completes_without_exemplars(account, user, monkeypatch, via, response):
    api, number, _sku = ordinary_order(account)
    unavailable = Mock(side_effect=OzonError('not applicable', status=400)) if response == 'refused' else Mock(return_value={})
    ship = Mock(side_effect=AssertionError('Готовый заказ нельзя собирать повторно'))
    monkeypatch.setattr(api, 'exemplars_get', unavailable)
    monkeypatch.setattr(api, 'ship', ship)
    result = scan(account, user, BARCODE if via == 'product' else number)
    assert result['action'] == 'posting_selected', result['message']
    assert result['state']['active']['status'] == 'awaiting_deliver'
    assert result['state']['done'] == (1 if via == 'product' else 0)
    if via == 'label':
        assert not result.get('print')
        scan(account, user, BARCODE)
    assert scan(account, user, number)['action'] == 'completed'
    unavailable.assert_not_called()
    ship.assert_not_called()
    assert marking.order(account['id'], number) is None


def test_fresh_ready_status_wins_over_old_local_packaging(account, user, monkeypatch):
    api, number, _sku = ordinary_order(account, status='awaiting_packaging')
    api._postings[number]['status'] = 'awaiting_deliver'
    ship = Mock(side_effect=AssertionError('Локальный статус устарел, ship вызывать нельзя'))
    monkeypatch.setattr(api, 'ship', ship)
    monkeypatch.setattr(api, 'exemplars_get', Mock(side_effect=AssertionError('Нет маркировки')))
    result = scan(account, user, number)
    assert result['status'] == 'ok', result['message']
    assert result['state']['active']['status'] == 'awaiting_deliver'
    ship.assert_not_called()


def test_ordinary_packaging_still_becomes_ready_without_exemplar_calls(account, user, monkeypatch):
    api, number, _sku = ordinary_order(account, status='awaiting_packaging')
    ship = Mock(wraps=api.ship)
    monkeypatch.setattr(api, 'ship', ship)
    monkeypatch.setattr(api, 'exemplars_get', Mock(side_effect=AssertionError('Нет маркировки')))
    result = scan(account, user, BARCODE)
    assert result['status'] == 'ok', result['message']
    assert api._postings[number]['status'] == 'awaiting_deliver'
    ship.assert_called_once()


def test_legacy_empty_marking_snapshot_does_not_keep_ordinary_order_blocked(account, user, monkeypatch):
    api, number, _sku = ordinary_order(account)
    with order_lock.hold(account['id'], number):
        saved = marking.prepare(account, number, for_scan=True)
    assert not marking.needs_validation(saved)
    monkeypatch.setattr(api, 'exemplars_get', Mock(side_effect=OzonError('not applicable', status=400)))
    result = scan(account, user, BARCODE)
    assert result['status'] == 'ok', result['message']
    assert scan(account, user, number)['action'] == 'completed'


@pytest.mark.parametrize('status', ['awaiting_packaging', 'awaiting_deliver'])
def test_required_marking_is_not_bypassed_when_exemplar_read_fails(account, user, monkeypatch, status):
    api, number, _sku = marked_ozon(account, status=status)
    unavailable = Mock(side_effect=OzonError('no permission', status=403))
    monkeypatch.setattr(api, 'exemplars_get', unavailable)
    ship = Mock(side_effect=AssertionError('ЧЗ ещё не проверен'))
    monkeypatch.setattr(api, 'ship', ship)
    result = scan(account, user, number)
    assert result['status'] == 'error'
    assert result['state']['active'] is None
    assert 'HTTP 403' in result['message'] and 'права' in result['message']
    unavailable.assert_called_once()
    ship.assert_not_called()


def test_known_requirement_survives_failed_read_after_ozon_clears_flag(account, user, monkeypatch):
    api, number, _sku = marked_ozon(account, status='awaiting_deliver')
    api._postings[number]['requirements']['products_requiring_mandatory_mark'] = []
    unavailable = Mock(side_effect=OzonError('temporary', status=500))
    monkeypatch.setattr(api, 'exemplars_get', unavailable)
    for _ in range(2):
        result = scan(account, user, number)
        assert result['status'] == 'error'
        assert result['state']['active'] is None
    assert unavailable.call_count == 2


def test_saved_codes_are_still_checked_after_remote_requirement_is_cleared(account, user, monkeypatch):
    api, number, _sku = marked_ozon(account, status='awaiting_deliver')
    scan(account, user, BARCODE)
    scan(account, user, code())
    finish_job(account, number)
    api._postings[number]['requirements']['products_requiring_mandatory_mark'] = []
    with db.write() as conn:
        store.upsert_posting(conn, account['id'], api._postings[number])
    monkeypatch.setattr(api, 'exemplars_get', Mock(side_effect=OzonError('temporary', status=500)))
    assert packing.complete(user)['status'] == 'error'
    row = db.query_one('SELECT local_state FROM postings WHERE account_id=? AND posting_number=?', (account['id'], number))
    assert row['local_state'] != 'packed'
    assert marking.codes(marking.order(account['id'], number), full=True)[0]['raw'] == code()


def test_optional_marking_is_discovered_only_when_a_mark_is_scanned(account, user, monkeypatch):
    api, number, _sku = ordinary_order(account)
    original = api.exemplars_get
    def optional(order_id):
        data = original(order_id)
        data['products'][0]['is_mandatory_mark_possible'] = True
        return data
    calls = Mock(side_effect=optional)
    monkeypatch.setattr(api, 'exemplars_get', calls)
    assert scan(account, user, BARCODE)['status'] == 'ok'
    calls.assert_not_called()
    assert scan(account, user, code())['action'] == 'marking_saved'
    assert calls.call_count == 1
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'


def test_changed_remote_composition_has_a_clear_error(account, user):
    api, number, _sku = ordinary_order(account)
    api._postings[number]['products'][0]['quantity'] = 2
    result = scan(account, user, number)
    assert result['status'] == 'error'
    assert 'Состав отправления Ozon изменился' in result['message']
    assert result['state']['active'] is None


def test_remote_order_that_has_left_assembly_cannot_be_opened(account, user, monkeypatch):
    api, number, _sku = ordinary_order(account)
    api._postings[number]['status'] = 'delivering'
    monkeypatch.setattr(api, 'exemplars_get', Mock(side_effect=AssertionError('Заказ уже не в сборке')))
    result = scan(account, user, number)
    assert result['status'] == 'error'
    assert 'недоступно для сборки' in result['message']
    assert result['state']['active'] is None


def test_unrelated_exemplars_cannot_replace_known_required_product(account, user, monkeypatch):
    api, number, _sku = marked_ozon(account, status='awaiting_deliver')
    wrong = deepcopy(api.exemplars_get(number))
    wrong['products'][0]['product_id'] = 999999999
    monkeypatch.setattr(api, 'exemplars_get', Mock(return_value=wrong))
    result = scan(account, user, number)
    assert result['status'] == 'error'
    assert 'Состав экземпляров Ozon' in result['message']
    assert result['state']['active'] is None
