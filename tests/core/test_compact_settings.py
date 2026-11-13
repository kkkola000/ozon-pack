"""Единая вкладка печати сохраняет настройки атомарно и соблюдает прежние права."""
import re

import pytest
from fastapi.testclient import TestClient

from app.core import accounts, db, marking, printers, product_labels
from app.core.security import hash_password
from app.main import app


@pytest.fixture
def client(sample_data):
    with TestClient(app, follow_redirects=False) as value:
        yield value


def login(client, *, packer=False):
    if packer:
        db.execute('INSERT INTO users(login,password_hash,role,active,created_at) VALUES(?,?,?,1,?)',
                   ('worker', hash_password('worker-test-123'), 'packer', db.now_iso()))
    client.post('/login', data={'login': 'worker' if packer else 'admin',
                               'password': 'worker-test-123' if packer else 'test-admin-pass'})
    page = client.get('/pack')
    return {'X-CSRF-Token': re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1)}


def test_legacy_printer_bookmark_has_one_destination(client):
    login(client)
    result = client.get('/printers')
    assert result.status_code == 303
    assert result.headers['location'] == '/settings/printing'
    assert 'id="printer-table"' in client.get(result.headers['location']).text


def test_packer_printing_view_never_contains_management_data(client):
    login(client, packer=True)
    accounts.update(accounts.default_account()['id'], client_id='private-client-id')
    result = client.get('/settings/printing?tab=staff')
    assert result.status_code == 200
    assert 'id="printer-table"' in result.text
    for private in ('private-client-id', 'data-account=', 'data-role-for=', 'id="new-login"', 'id="print-marking"'):
        assert private not in result.text
    assert client.get('/settings?tab=staff').status_code == 403
    assert client.get('/settings?tab=accounts').status_code == 403


def test_printing_and_flags_are_saved_together(client):
    headers = login(client)
    response = client.post('/api/printers', headers=headers, json={
        'rows': [{'kind': 'ozon:label', 'size': '58x40', 'printer': 'LABEL', 'orientation': 'landscape',
                  'gap': 2, 'shift': {'right': 2}},
                 {'kind': 'marking:duplicate', 'size': '58x40', 'printer': 'CHZ'}],
        'formats': {'ozon:label': 'small'}, 'print_product_barcodes': True, 'print_marking_duplicates': True})
    assert response.status_code == 200, response.text
    assert product_labels.enabled() and marking.enabled()
    label = next(r for r in printers.rows() if r['kind'] == 'ozon:label')
    assert label['printer'] == 'LABEL' and label['orientation'] == 'landscape'
    assert label['gap'] == 2 and label['shift'] == {'right': 2}
    assert next(r for r in printers.rows() if r['kind'] == 'marking:duplicate')['printer'] == 'CHZ'
    assert printers.label_format('ozon:label') == 'small'


@pytest.mark.parametrize('changes', [
    {'formats': {'ozon:label': 'not-a-format'}},
    {'print_product_barcodes': 'true'},
    {'print_marking_duplicates': 1},
])
def test_invalid_batch_does_not_partially_change_printers_or_flags(client, changes):
    headers = login(client)
    printers.save([{'kind': 'ozon:label', 'size': '58x40', 'printer': 'ORIGINAL'}])
    original = {r['key']: r['value'] for r in db.query('SELECT * FROM kv')}
    payload = {'rows': [{'kind': 'ozon:label', 'size': '58x40', 'printer': 'NEW'}],
               'formats': {'ozon:label': 'small'}, 'print_product_barcodes': True,
               'print_marking_duplicates': True, **changes}
    assert client.post('/api/printers', headers=headers, json=payload).status_code == 400
    assert {r['key']: r['value'] for r in db.query('SELECT * FROM kv')} == original


@pytest.mark.parametrize('flag', ['print_product_barcodes', 'print_marking_duplicates'])
def test_packer_cannot_change_global_flags_through_batch_endpoint(client, flag):
    headers = login(client, packer=True)
    response = client.post('/api/printers', headers=headers, json={
        'rows': [{'kind': 'ozon:label', 'size': '58x40', 'printer': 'BAD'}], flag: True})
    assert response.status_code == 403
    assert all(r['printer'] != 'BAD' for r in printers.rows())
    assert not product_labels.enabled() and not marking.enabled()


def test_printing_tab_keeps_common_marking_printer_and_removes_left_logo(client):
    login(client)
    html = client.get('/settings').text
    assert 'class="brand"' not in html
    nav = re.search(r'<nav id="main-navigation".*?</nav>', html, re.S).group(0)
    assert 'Настройки' not in nav
    assert 'class="account-menu' in html and 'href="/settings"' in html
    assert html.count('class="printer-row" data-kind="marking:duplicate"') == 1
    assert 'data-add-kind="marking:duplicate"' not in html
    assert 'id="print-product-barcodes"' in html and 'id="print-marking"' in html
