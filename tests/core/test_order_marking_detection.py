"""Detect requirements from every opened order, independent of catalog access."""
from unittest.mock import Mock

import httpx
import pytest

from app.core import db, marking, packing
from app.markets.ozon.client import OzonClient, OzonError
from app.markets.yandex.client import YandexError
from tests.core.test_marking import BARCODE, code, finish_job, marked_yandex, scan
from tests.markets.ozon.test_ready_marking import ordinary_order


@pytest.mark.parametrize('status', ['awaiting_packaging', 'awaiting_deliver'])
@pytest.mark.parametrize('flag', ['is_mandatory_mark_needed', 'is_mandatory_mark_possible', 'is_gtd_needed'])
@pytest.mark.parametrize('via', ['product', 'label'])
def test_ozon_checks_requirements_without_preliminary_hints_or_catalog_rights(account, user, monkeypatch, status, flag, via):
    api, number, sku = ordinary_order(account, status=status)
    api._postings[number]['available_actions'] = ['ship']
    original = api.exemplars_get
    def modern(order_id):
        data = original(order_id)
        data['products'][0][flag] = True
        return data
    read = Mock(side_effect=modern)
    monkeypatch.setattr(api, 'exemplars_get', read)
    attrs = Mock(side_effect=OzonError('catalog forbidden', status=403))
    definitions = Mock(side_effect=OzonError('category forbidden', status=403))
    monkeypatch.setattr(api, 'product_attributes', attrs, raising=False)
    monkeypatch.setattr(api, 'category_attributes', definitions, raising=False)
    result = scan(account, user, BARCODE if via == 'product' else number)
    assert result['status'] == 'ok', result
    assert read.called
    attrs.assert_not_called()
    definitions.assert_not_called()
    state = result['state']
    assert not state['complete']
    assert packing.complete(user)['status'] == 'error'
    if flag == 'is_gtd_needed':
        assert not state['items'][0].get('marking')
        assert state['items'][0]['exemplar_data']['units'][0]['fields'][0]['name'] == 'gtd'
    else:
        assert state['items'][0]['marking']['status'] == 'waiting'
        if via == 'label':
            scan(account, user, BARCODE)
        assert packing.state(user)['marking']['pending_key'] == sku
        assert scan(account, user, code())['action'] == 'marking_saved'
        finish_job(account, number)
        assert marking.order(account['id'], number)['status'] == 'accepted'


@pytest.mark.parametrize('ready', [False, True])
@pytest.mark.parametrize('kind', ['CIS', 'CIS_OPTIONAL', 'GTD', 'RNPT', 'UIN'])
def test_yandex_order_types_control_collection_without_offer_access(yandex_account, user, monkeypatch, ready, kind):
    api, number, key = marked_yandex(yandex_account)
    raw = api._orders[number]
    raw['substatus'] = 'READY_TO_SHIP' if ready else 'STARTED'
    raw['items'][0]['requiredInstanceTypes'] = [kind]
    catalog = Mock(side_effect=YandexError('catalog forbidden', status=403))
    monkeypatch.setattr(api, 'offer_mappings', catalog)
    read = Mock(wraps=api.order_details)
    monkeypatch.setattr(api, 'order_details', read)
    result = scan(yandex_account, user, BARCODE)
    assert result['status'] == 'ok', result
    assert read.called
    catalog.assert_not_called()
    state = result['state']
    if kind == 'UIN':
        assert state['complete'] and not state['items'][0].get('exemplar_data')
        assert not state['items'][0].get('marking')
        assert scan(yandex_account, user, number)['action'] == 'completed'
        return
    assert not state['complete']
    assert packing.complete(user)['status'] == 'error'
    if kind in {'CIS', 'CIS_OPTIONAL'}:
        assert state['items'][0]['marking']['status'] == 'waiting'
        assert state['marking']['pending_key'] == key
    else:
        assert not state['items'][0].get('marking')
        assert state['items'][0]['exemplar_data']['units'][0]['fields'][0]['name'] == kind.lower()


def test_stale_catalog_values_do_not_replace_current_order_requirements(account, user, monkeypatch):
    api, number, sku = ordinary_order(account)
    db.execute('UPDATE products SET marking_required=1,marking_checked_at=? WHERE account_id=? AND sku=?',
               (db.now_iso(), account['id'], sku))
    read = Mock(wraps=api.exemplars_get)
    monkeypatch.setattr(api, 'exemplars_get', read)
    opened = scan(account, user, BARCODE)
    assert opened['state']['complete']
    assert not opened['state']['items'][0].get('marking')
    assert read.called
    assert scan(account, user, number)['action'] == 'completed'


def test_gtd_requirement_survives_disappearing_flags_after_submission(account, user):
    from tests.core.test_exemplar_data import GTD, enter, require_ozon
    api, number, sku = require_ozon(account, cz=False)
    scan(account, user, BARCODE)
    enter(account, user, number, sku, {'gtd': GTD})
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'
    api._postings[number]['requirements'] = {}
    assert scan(account, user, number)['action'] == 'completed'


def test_real_ozon_client_requests_exemplar_flags_by_posting_number():
    import json
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={'posting_number': '51121561-0339-2', 'products': []})
    api = OzonClient('client', 'key')
    api._client.close()
    api._client = httpx.Client(base_url='https://api-seller.ozon.ru', transport=httpx.MockTransport(handle))
    try:
        api.exemplars_get('51121561-0339-2')
        assert len(requests) == 1
        assert requests[0].url.path == '/v6/fbs/posting/product/exemplar/create-or-get'
        assert json.loads(requests[0].content) == {'posting_number': '51121561-0339-2'}
    finally:
        api.close()


@pytest.mark.parametrize('market', ['ozon', 'yandex'])
@pytest.mark.parametrize('kind', ['cz', 'gtd'])
def test_next_scan_refreshes_requirements_in_an_already_open_order(account, yandex_account, user, monkeypatch, market, kind):
    if market == 'ozon':
        shop = account
        api, number, key = ordinary_order(shop)
    else:
        shop = yandex_account
        api, number, key = marked_yandex(shop)
        api._orders[number]['items'][0]['requiredInstanceTypes'] = []
    opened = scan(shop, user, BARCODE)
    assert opened['state']['complete'] and not opened['state']['items'][0].get('marking')
    if market == 'ozon':
        original = api.exemplars_get
        def newly_required(order_id):
            data = original(order_id)
            data['products'][0]['is_mandatory_mark_possible' if kind == 'cz' else 'is_gtd_needed'] = True
            return data
        monkeypatch.setattr(api, 'exemplars_get', newly_required)
    else:
        api._orders[number]['items'][0]['requiredInstanceTypes'] = ['CIS_OPTIONAL' if kind == 'cz' else 'GTD']
    result = scan(shop, user, BARCODE)
    assert not result['state']['complete']
    if kind == 'cz':
        assert result['status'] == 'error' and 'Честный знак' in result['message']
        assert result['state']['items'][0]['marking']['status'] == 'waiting'
        assert result['state']['marking']['pending_key'] == key
    else:
        assert result['state']['items'][0]['exemplar_data']['units'][0]['fields'][0]['name'] == 'gtd'
    assert not marking.codes(marking.order(shop['id'], number))


def test_failed_requirement_refresh_does_not_credit_another_product(account, user, monkeypatch):
    from tests.core.test_marking import marked_ozon
    from app.markets.ozon import store
    api, number, key = marked_ozon(account, quantity=2, status='awaiting_deliver')
    api._postings[number]['requirements'] = {}
    with db.write() as conn:
        store.upsert_posting(conn, account['id'], api._postings[number])
    assert scan(account, user, BARCODE)['state']['done'] == 1
    monkeypatch.setattr(api, 'exemplars_get', Mock(side_effect=OzonError('sensitive body', status=403)))
    failed = scan(account, user, BARCODE)
    assert failed['status'] == 'error' and 'create-or-get' in failed['message']
    assert failed['state']['done'] == 1
    assert 'sensitive body' not in str(failed)
    assert packing.complete(user)['status'] == 'error'
