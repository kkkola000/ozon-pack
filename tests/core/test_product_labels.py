"""Приоритет товарных ШК и порядок страниц перед оригинальным стикером."""
import io
import json
import re

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

from app.core import db, product_labels
from app.main import app
from app.markets import registry



@pytest.fixture(autouse=True)
def enable_product_labels(temp_db):
    db.kv_set(product_labels.KV_ENABLED, '1')

def product(account_id, sku, codes):
    db.execute('INSERT INTO products(account_id,sku,name,barcodes) VALUES(?,?,?,?) '
               'ON CONFLICT(account_id,sku) DO UPDATE SET barcodes=excluded.barcodes',
               (account_id, str(sku), 'Товар', json.dumps(codes)))


def shipment(pages=2, width=164.4, height=113.4):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=width, height=height)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.mark.parametrize('prefix,codes,expected', [
    ('OZN', ['460123', 'MRKT999', 'OZN123'], 'OZN123'),
    ('MRKT', ['OZN123', '460123', 'MRKT999'], 'MRKT999'),
    ('OZN', ['460123', '460999'], '460123'),
    ('', ['460123', '460999'], '460123'),
    ('OZN', ['ozn123', '460123'], 'ozn123'),
])
def test_market_prefix_has_priority_in_own_catalog(account, prefix, codes, expected):
    product(account['id'], 'own', codes)
    assert product_labels.choose(account['id'], 'own', prefix) == expected


def test_main_card_fallback_excludes_both_market_prefixes(account, avito_account):
    product(avito_account['id'], 'source', [])
    product(account['id'], 'main', ['OZN123', 'mrkt999', '4600012345678'])
    for shop, sku, main in [(avito_account['id'], 'source', 0), (account['id'], 'main', 1)]:
        db.execute('INSERT INTO product_links(group_id,account_id,sku,is_main) VALUES(?,?,?,?)', ('g', shop, sku, main))
    assert product_labels.choose(avito_account['id'], 'source') == '4600012345678'
    product(account['id'], 'main', ['OZN123', 'MRKT999'])
    assert product_labels.choose(avito_account['id'], 'source') is None
    product(avito_account['id'], 'source', ['LOCAL-AVITO'])
    assert product_labels.choose(avito_account['id'], 'source') == 'LOCAL-AVITO'


def test_unlinked_same_sku_in_other_account_is_not_used(account, avito_account):
    product(account['id'], 'same', ['OZN123'])
    assert product_labels.choose(avito_account['id'], 'same', 'OZN') is None


def test_codes_in_index_are_available_but_invalid_characters_are_not_removed(account):
    db.execute('INSERT INTO product_barcodes(account_id,sku,barcode) VALUES(?,?,?)',
               (account['id'], 'indexed', '460000001'))
    assert product_labels.choose(account['id'], 'indexed') == '460000001'
    product(account['id'], 'bad', ['OZN\n123', 'ШТРИХКОД'])
    assert product_labels.choose(account['id'], 'bad', 'OZN') is None


@pytest.mark.parametrize('width,height', [(164.4, 113.4), (212.6, 340.2)])
def test_product_pages_come_first_and_keep_shipping_paper(account, monkeypatch, width, height):
    monkeypatch.setattr(product_labels, 'codes_for_order', lambda *_: ['OZN123', 'OZN123', '460000001'])
    source = shipment(width=width, height=height)
    result = PdfReader(io.BytesIO(product_labels.prepend(account, '1', source)))
    assert len(result.pages) == 5
    assert [page.extract_text().strip() for page in result.pages[:3]] == ['OZN123', 'OZN123', '460000001']
    assert all(not page.extract_text() for page in result.pages[3:])
    for page in result.pages:
        assert float(page.mediabox.width) == pytest.approx(width, abs=.02)
        assert float(page.mediabox.height) == pytest.approx(height, abs=.02)


def test_missing_barcode_returns_original_file_without_extra_pages(account, monkeypatch):
    monkeypatch.setattr(product_labels, 'codes_for_order', lambda *_: [])
    source = shipment()
    assert product_labels.prepend(account, '1', source) is source


@pytest.mark.parametrize('market', ['ozon', 'avito', 'yandex'])
def test_real_order_adapters_preserve_own_sku_and_quantities(account, avito_account, yandex_account, sample_data, market):
    from app.core import sync

    shop = {'ozon': account, 'avito': avito_account, 'yandex': yandex_account}[market]
    if market != 'ozon':
        sync.sync_account(shop)
    source = registry.get(market).labels
    # Для проверки адаптера подходит заказ с позициями в любом рабочем статусе.
    row = db.query_one(f'SELECT {source.key} AS id FROM {source.table} WHERE account_id = ? LIMIT 1', (shop['id'],))
    items = source.items(shop['id'], str(row['id']))
    assert items
    expected = []
    for i, item in enumerate(items):
        code = (source.barcode_prefix or 'OWN') + str(i)
        product(shop['id'], item['sku'], ['460000000', code] if source.barcode_prefix else [code])
        expected += [code] * item['quantity']
    assert product_labels.codes_for_order(shop, str(row['id'])) == expected


@pytest.mark.parametrize('market', ['ozon', 'avito', 'yandex'])
def test_prepared_print_keeps_archive_original_and_requires_csrf(account, avito_account, yandex_account, sample_data, market):
    from app.core import sync

    account = {'ozon': account, 'avito': avito_account, 'yandex': yandex_account}[market]
    if market != 'ozon':
        sync.sync_account(account)
    source = registry.get(market).labels
    key = source.pending(account['id'])[0]
    first = source.items(account['id'], key)[0]
    preferred = (source.barcode_prefix or 'OWN') + '-PREFERRED'
    product(account['id'], first['sku'], ['46000000', preferred] if source.barcode_prefix else [preferred])
    with TestClient(app, follow_redirects=False) as client:
        client.post('/login', data={'login': 'admin', 'password': 'test-admin-pass'})
        page = client.get('/pack')
        csrf = re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1)
        payload = {'account_id': account['id'], 'ids': [key], 'with_products': True}
        assert client.post('/api/orders/labels.pdf', json=payload).status_code == 403
        response = client.post('/api/orders/labels.pdf', json=payload, headers={'X-CSRF-Token': csrf})
        assert response.status_code == 200, response.text
        printed = PdfReader(io.BytesIO(response.content))
        assert printed.pages[0].extract_text().strip() == preferred
        archived = db.query_one('SELECT v.pdf FROM sticker_versions v JOIN sticker_orders o ON o.id=v.archive_id '
                                'WHERE o.account_id=? AND o.order_id=? ORDER BY v.id DESC', (account['id'], key))
        original = PdfReader(io.BytesIO(archived['pdf']))
        assert len(printed.pages) == len(original.pages) + len(product_labels.codes_for_order(account, key))
        assert preferred not in ''.join(page.extract_text() or '' for page in original.pages)
        response = client.get(f'/api/pack/label/{market}/{key}.pdf?with_products=true')
        assert response.status_code == 200, response.text
        assert PdfReader(io.BytesIO(response.content)).pages[0].extract_text().strip() == preferred


def test_setting_is_off_by_default_and_prepend_is_unchanged(account):
    db.execute('DELETE FROM kv WHERE key = ?', (product_labels.KV_ENABLED,))
    assert not product_labels.enabled()
    source = shipment()
    assert product_labels.prepend(account, '1', source) is source


def test_setting_requires_manager_and_csrf_and_persists(account, sample_data):
    from app.core import security

    db.kv_set(product_labels.KV_ENABLED, '0')
    with TestClient(app, follow_redirects=False) as client:
        client.post('/login', data={'login': 'admin', 'password': 'test-admin-pass'})
        page = client.get('/settings')
        csrf = re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1)
        assert 'id="print-product-barcodes"' in page.text
        assert client.post('/api/settings/product-barcodes', json={'enabled': True}).status_code == 403
        headers = {'X-CSRF-Token': csrf}
        assert client.post('/api/settings/product-barcodes', json={'enabled': 'yes'}, headers=headers).status_code == 400
        assert client.post('/api/settings/product-barcodes', json={'enabled': True}, headers=headers).status_code == 200
        db.init_db()
        assert product_labels.enabled()
        assert client.post('/api/settings/product-barcodes', json={'enabled': False}, headers=headers).status_code == 200
        assert not product_labels.enabled()
        db.execute('INSERT INTO users(login,password_hash,role,sections,active,created_at) VALUES(?,?,?,?,1,?)',
                   ('packer', security.hash_password('test-packer-password'), 'packer', '["pack"]', db.now_iso()))
        client.post('/login', data={'login': 'packer', 'password': 'test-packer-password'})
        assert client.post('/api/settings/product-barcodes', json={'enabled': True}, headers=headers).status_code == 403


def test_disabled_setting_skips_product_pages_even_with_prepared_url(account, sample_data):
    db.kv_set(product_labels.KV_ENABLED, '0')
    source = registry.get('ozon').labels
    key = source.pending(account['id'])[0]
    first = source.items(account['id'], key)[0]
    product(account['id'], first['sku'], ['OZN-SHOULD-NOT-PRINT'])
    with TestClient(app, follow_redirects=False) as client:
        client.post('/login', data={'login': 'admin', 'password': 'test-admin-pass'})
        response = client.get(f'/api/pack/label/ozon/{key}.pdf?with_products=true')
        assert response.status_code == 200
        assert 'OZN-SHOULD-NOT-PRINT' not in ''.join(page.extract_text() or '' for page in PdfReader(io.BytesIO(response.content)).pages)
