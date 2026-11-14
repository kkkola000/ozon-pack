"""Marketplace declarations remain separate from GTD and from barcodes."""
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from app.core import catalog, db, marking, packing, product_marking
from app.markets.ozon import client as ozon_client
from tests.core.test_marking import BARCODE, marked_yandex, scan
from tests.markets.ozon.test_ready_marking import ordinary_order


@pytest.mark.parametrize('status', ['awaiting_packaging', 'awaiting_deliver'])
def test_ozon_product_card_requires_cz_without_old_order_flags(account, user, monkeypatch, status):
    api, number, sku = ordinary_order(account, status=status)
    monkeypatch.setattr(api, 'product_attributes', lambda _skus: [
        {'sku': int(sku), 'description_category_id': 123, 'type_id': 456,
         'attributes': [{'id': 999, 'values': [{'dictionary_value_id': 100, 'value': 'Да'}]}]}])
    monkeypatch.setattr(api, 'category_attributes', lambda *_args: [{'id': 999, 'name': 'Нужна маркировка'}])
    result = scan(account, user, BARCODE)
    assert result['status'] == 'ok'
    assert result['state']['items'][0]['marking']['status'] == 'waiting'
    assert marking.has_required(marking.order(account['id'], number))
    assert packing.complete(user)['status'] == 'error'


def test_yandex_declared_card_requires_cz_without_cis_requirement(yandex_account, user, monkeypatch):
    api, number, key = marked_yandex(yandex_account)
    raw = api._orders[number]
    raw['items'][0]['requiredInstanceTypes'] = []
    sku = raw['items'][0]['offerId']
    monkeypatch.setattr(api, 'offer_mappings', lambda **_kwargs: ([{'offer': {'offerId': sku, 'markingDeclared': True}}], None))
    state = scan(yandex_account, user, BARCODE)['state']
    assert state['items'][0]['marking']['status'] == 'waiting'
    assert not state['complete']
    assert marking.has_required(marking.order(yandex_account['id'], number))


def test_unknown_card_never_overwrites_true_and_reads_are_cached(account):
    db.execute('INSERT INTO products(account_id,sku,marking_required) VALUES(?,?,1)', (account['id'], '42'))
    read = Mock(return_value=[])
    api = SimpleNamespace(product_attributes=read)
    assert product_marking.declarations(account, ['42'], api) == {'42'}
    assert product_marking.declarations(account, ['42'], api) == {'42'}
    read.assert_called_once()
    with db.write() as conn:
        catalog.save(conn, account['id'], [{'sku': '42', 'name': 'Changed', 'barcodes': []}])
    assert db.query_one('SELECT marking_required FROM products WHERE sku=?', ('42',))['marking_required'] == 1


def test_dictionary_id_does_not_mean_yes_and_names_are_not_guessed(account):
    api = SimpleNamespace(product_attributes=lambda _: [
        {'sku': 42, 'description_category_id': 123, 'type_id': 456,
         'attributes': [{'id': 999, 'values': [{'dictionary_value_id': 100, 'value': 'Нет'}]},
                        {'id': 777, 'values': [{'value': 'Да'}]}]}],
        category_attributes=lambda *_: [{'id': 999, 'name': 'Нужна маркировка'}, {'id': 777, 'name': 'Нужна упаковка'}])
    assert product_marking.declarations(account, ['42'], api) == set()
    assert db.query_one('SELECT marking_required FROM products WHERE sku=?', ('42',))['marking_required'] == 0


def test_real_client_uses_documented_attribute_endpoints():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={'result': []})
    api = ozon_client.OzonClient('client', 'key')
    api._client.close()
    api._client = httpx.Client(base_url='https://api-seller.ozon.ru', transport=httpx.MockTransport(handle))
    try:
        api.product_attributes(['42'])
        api.category_attributes(123, 456)
        assert [r.url.path for r in requests] == ['/v4/product/info/attributes', '/v1/description-category/attribute']
        import json
        assert json.loads(requests[0].content)['filter']['sku'] == ['42']
        assert json.loads(requests[1].content)['type_id'] == 456
    finally:
        api.close()
