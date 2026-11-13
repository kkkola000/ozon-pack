"""Автоскачивание: правильный кабинет, оригинальный PDF, прежний архив и ЧЗ."""
import io
import hashlib
import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.core import accounts, assembly_stickers, board, db, security, sync
from app.main import app
from app.markets import registry
from tests.pdfstub import make_label_pdf


@pytest.fixture
def warehouse(sample_data, avito_account, yandex_account):
    sync.sync_all()
    return {a['marketplace']: a for a in accounts.all_accounts()}


@pytest.fixture
def client(warehouse):
    with TestClient(app, follow_redirects=False) as client:
        client.post('/login', data={'login': 'admin', 'password': 'test-admin-pass'})
        token = re.search(r'name="csrf-token" content="([^"]*)"', client.get('/pack').text).group(1)
        client.headers['X-CSRF-Token'] = token
        yield client


def order(account, status='deliver'):
    return str(board.rows([account], status)[0]['id'])


def source_response(monkeypatch, account, callback):
    original = registry.get
    market = original(account['marketplace'])
    modified = replace(market, labels=replace(market.labels, pdf=callback, files=None))
    monkeypatch.setattr(registry, 'get', lambda code: modified if code == market.code else original(code))


@pytest.mark.parametrize('market', ['ozon', 'yandex', 'avito'])
def test_single_download_saves_original_without_counting_a_print(client, warehouse, market):
    account = warehouse[market]
    number = order(account)
    job = assembly_stickers.descriptor(account, number)
    assert job['ready']
    response = client.get(job['url'])
    assert response.status_code == 200, response.text
    assert response.headers['content-type'] == 'application/pdf'
    assert response.headers['content-disposition'] == f'attachment; filename="{job["filename"]}"'
    assert len(PdfReader(io.BytesIO(response.content)).pages) >= 1
    source = registry.get(market).labels
    row = db.query_one(f'SELECT printed_at, print_count, label_saved_at FROM {source.table} '
                       f'WHERE account_id=? AND {source.key}=?', (account['id'], number))
    assert row['print_count'] == 0 and row['printed_at'] is None
    assert row['label_saved_at']
    stored = db.query_one('SELECT v.pdf FROM sticker_versions v JOIN sticker_orders o ON o.id=v.archive_id '
                          'WHERE o.account_id=? AND o.order_id=?', (account['id'], number))
    assert bytes(stored['pdf']) == response.content


@pytest.mark.parametrize('legacy_history', [False, True])
def test_download_uses_existing_archive_and_never_adds_a_version(client, warehouse, monkeypatch, legacy_history):
    account = warehouse['ozon']
    number = order(account)
    url = assembly_stickers.descriptor(account, number)['url']
    first = client.get(url)
    assert first.status_code == 200
    expected = first.content
    if legacy_history:
        # Старые версии уже могут быть в БД; раздел «Стикеры» показывает последнюю.
        expected = make_label_pdf([{'posting_number': 'LATEST-LEGACY-STICKER'}])
        saved = db.query_one('SELECT * FROM sticker_versions ORDER BY id DESC LIMIT 1')
        db.execute('INSERT INTO sticker_versions(archive_id,account_id,saved_at,digest,pdf) VALUES(?,?,?,?,?)',
                   (saved['archive_id'], account['id'], db.now_iso(), hashlib.sha256(expected).hexdigest(), expected))
    before = [dict(row) for row in db.query('SELECT * FROM sticker_versions')]

    def unavailable(*args):
        raise RuntimeError('Marketplace is unavailable')

    source_response(monkeypatch, account, unavailable)
    again = client.get(url)
    assert again.status_code == 200 and again.content == expected
    assert [dict(row) for row in db.query('SELECT * FROM sticker_versions')] == before


@pytest.mark.parametrize('market', ['ozon', 'yandex'])
def test_packaging_order_waits_for_verified_readiness(client, warehouse, monkeypatch, market):
    account = warehouse[market]
    number = order(account, 'packaging')

    def premature(*args):
        raise AssertionError('Нельзя запрашивать стикер до передачи ЧЗ и готовности')

    source_response(monkeypatch, account, premature)
    job = assembly_stickers.descriptor(account, number)
    assert job['ready'] is False
    assert client.get(job['url']).status_code == 409
    assert board.not_ready(account, [number]) == [number]


def test_all_pages_of_a_multibox_sticker_are_downloaded(client, warehouse, monkeypatch):
    account = warehouse['ozon']
    number = order(account)
    pdf = make_label_pdf([{'posting_number': 'BOX-ONE'}, {'posting_number': 'BOX-TWO'}])
    source_response(monkeypatch, account, lambda *_args: pdf)
    result = client.get(assembly_stickers.descriptor(account, number)['url'])
    assert result.status_code == 200 and result.content == pdf
    assert len(PdfReader(io.BytesIO(result.content)).pages) == 2


@pytest.mark.parametrize('content', [None, b'<html>Login</html>', b'%PDF-broken'])
def test_bad_pdf_does_not_get_marked_as_downloaded(client, warehouse, monkeypatch, content):
    account = warehouse['ozon']
    number = order(account)
    source_response(monkeypatch, account, lambda *_args: content)
    response = client.get(assembly_stickers.descriptor(account, number)['url'])
    assert response.status_code == 502
    assert db.query_one('SELECT label_saved_at FROM postings WHERE account_id=? AND posting_number=?',
                        (account['id'], number))['label_saved_at'] is None


def test_account_in_url_cannot_return_another_cabinets_sticker(client, warehouse):
    number = order(warehouse['ozon'])
    other = accounts.create('ozon', 'Другой кабинет', 'another-client', 'another-key')
    assert client.get(f'/api/pack/sticker/{other}/{number}.pdf').status_code == 404
    assert client.get(f'/api/pack/sticker/999999/{number}.pdf').status_code == 404
    assert assembly_stickers.filename(warehouse['ozon'], number) != assembly_stickers.filename(accounts.get(other), number)


def test_error_from_marketplace_does_not_expose_its_details(client, warehouse, monkeypatch):
    account = warehouse['ozon']

    def fail(*args):
        raise RuntimeError('private-api-token')

    source_response(monkeypatch, account, fail)
    result = client.get(assembly_stickers.descriptor(account, order(account))['url'])
    assert result.status_code == 502
    assert 'private-api-token' not in result.text


def test_download_requires_login_and_pack_access(client, warehouse):
    url = assembly_stickers.descriptor(warehouse['ozon'], order(warehouse['ozon']))['url']
    client.get('/logout')
    assert client.get(url).status_code == 401
    db.execute('INSERT INTO users(login,password_hash,role,sections,active,created_at) VALUES(?,?,?,?,1,?)',
               ('restricted', security.hash_password('restricted-pass-123'), 'packer', '[]', db.now_iso()))
    client.post('/login', data={'login': 'restricted', 'password': 'restricted-pass-123'})
    assert client.get(url).status_code == 403
