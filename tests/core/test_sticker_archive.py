"""Архив не зависит от доступности площадок после отгрузки и хранит все листы."""
from datetime import datetime, timezone
import io
import re
import zipfile

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

from app.core import access, accounts, db, security, sticker_archive as archive
from app.core.config import settings
from app.main import app
from app.markets import registry


def pdf(pages=1):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=300)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def queued(account, key='order-1', number=None):
    return db.execute(
        'INSERT INTO sticker_orders(account_id, marketplace, shop_title, order_id, order_number, created_at) '
        'VALUES(?,?,?,?,?,?)',
        (account['id'], account['marketplace'], account['title'], key, number or key, db.now_iso()),
    ).lastrowid


def sign_in(client, login='admin', password='test-admin-pass'):
    assert client.post('/login', data={'login': login, 'password': password}).status_code == 303
    page = client.get('/reports/stickers')
    return re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1) if page.status_code == 200 else ''


def test_versions_preserve_all_pages_and_reversions(account):
    order = queued(account)
    first, changed = pdf(2), pdf(3)
    one = archive.save(order, first)
    assert archive.save(order, first) == one
    two = archive.save(order, changed)
    three = archive.save(order, first)
    assert one < two < three
    assert len(PdfReader(io.BytesIO(archive.version(one)['pdf'])).pages) == 2
    items, total = archive.search()
    assert total == 1 and items[0]['version_id'] == three and items[0]['versions'] == 3


@pytest.mark.parametrize('body', [b'not-pdf', b'%PDF-broken'])
def test_invalid_file_does_not_replace_saved_pdf(account, body):
    order = queued(account)
    saved = archive.save(order, pdf())
    with pytest.raises(ValueError):
        archive.save(order, body)
    assert archive.search()[0][0]['version_id'] == saved


def test_calendar_retention_and_expired_file_access(account):
    assert archive.cutoff(datetime(2026, 8, 31, 12, tzinfo=timezone.utc)) == '2026-02-28T12:00:00+00:00'
    order = queued(account)
    old = archive.save(order, pdf())
    db.execute("UPDATE sticker_versions SET saved_at = '2000-01-01T00:00:00+00:00'")
    db.execute("UPDATE sticker_orders SET created_at = '2000-01-01T00:00:00+00:00'")
    assert archive.version(old) is None
    assert archive.search()[1] == 0
    archive.purge()
    assert not db.query('SELECT * FROM sticker_orders')
    assert not db.query('SELECT * FROM sticker_versions')


def test_expiring_old_version_preserves_new_one(account):
    order = queued(account)
    first = archive.save(order, pdf())
    second = archive.save(order, pdf(2))
    db.execute("UPDATE sticker_versions SET saved_at = '2000-01-01' WHERE id = ?", (first,))
    archive.purge()
    assert archive.version(second) and not archive.version(first)
    assert archive.search()[0][0]['versions'] == 1


def test_sync_only_enqueues_and_background_captures(account, sample_data, monkeypatch):
    assert archive.search()[1] > 0
    assert not db.query('SELECT * FROM sticker_versions')
    calls = []
    original = archive.capture

    def capture(shop, order, key):
        calls.append(key)
        return original(shop, order, key)

    monkeypatch.setattr(archive, 'capture', capture)
    archive.run_once()
    assert calls and db.query('SELECT * FROM sticker_versions')
    # Фоновое сохранение не снимает замок выгрузки сборщика.
    assert registry.get('ozon').labels.pending(account['id'])
    count = len(calls)
    archive.run_once()
    assert len(calls) == count


@pytest.mark.parametrize('market', ['ozon', 'avito', 'yandex'])
def test_capture_keeps_multibox_original_for_every_market(account, market, monkeypatch):
    from dataclasses import replace

    shop = dict(account, marketplace=market)
    order = queued(shop)
    body = pdf(3)
    source = registry.get(market).labels
    if source.files:
        source = replace(source, files=lambda *_: {'order-1': body})
    else:
        source = replace(source, pdf=lambda *_: body)
    monkeypatch.setattr(archive, '_source', lambda _: replace(registry.get(market), labels=source))
    archive.capture(shop, order, 'order-1')
    assert archive.search()[0][0]['versions'] == 1
    assert db.query_one('SELECT pdf FROM sticker_versions')['pdf'] == body


def test_download_failure_is_retained_then_retried(account, sample_data, monkeypatch):
    def fail(*args):
        raise RuntimeError('sensitive upstream response')

    monkeypatch.setattr(archive, 'capture', fail)
    archive.run_once()
    items, total = archive.search(state='error')
    assert total and all('sensitive' not in row['error'] for row in items)
    original_attempt = items[0]['checked_at']
    # Нельзя бесконечно долбить отказавшую площадку.
    archive.run_once()
    assert archive.search(state='error')[0][0]['checked_at'] == original_attempt
    archive.retry(items[0]['id'])
    assert db.query_one('SELECT checked_at FROM sticker_orders WHERE id = ?', (items[0]['id'],))['checked_at'] is None


def test_departed_order_is_not_refetched_but_pdf_is_available(account, sample_data, monkeypatch):
    archive.run_once()
    items, _ = archive.search()
    saved = items[0]['version_id']
    monkeypatch.setattr(archive, '_ready', lambda _: [])
    monkeypatch.setattr(archive, 'capture', lambda *_: pytest.fail('departed order refetched'))
    archive.run_once()
    assert archive.version(saved)
    with pytest.raises(ValueError, match='больше не ожидает'):
        archive.retry(items[0]['id'])


def test_same_order_number_from_different_accounts_stays_separate(account, avito_account):
    for shop in [account, avito_account]:
        archive.save(queued(shop), pdf())
    assert archive.search()[1] == 2
    assert archive.search(account_id=account['id'])[1] == 1
    assert archive.search(market='avito')[1] == 1
    assert archive.search(q="' OR 1=1 --")[1] == 0
    assert archive.search(q='ORDER-1')[1] == 2


def test_archive_routes_download_zip_and_csrf(account):
    first = archive.save(queued(account), pdf(2))
    second = archive.save(queued(account, 'order-2'), pdf())
    with TestClient(app, follow_redirects=False) as client:
        assert client.get(f'/api/stickers/{first}.pdf').status_code == 401
        csrf = sign_in(client)
        page = client.get('/reports/stickers')
        assert page.status_code == 200 and 'reports-menu' in page.text and 'order-1' in page.text
        assert client.get('/reports').status_code == 200
        assert client.get('/reports/returns').status_code == 200
        response = client.get(f'/api/stickers/{first}.pdf?download=true')
        assert response.status_code == 200 and response.headers['content-disposition'].startswith('attachment;')
        assert len(PdfReader(io.BytesIO(response.content)).pages) == 2
        assert client.post('/api/stickers/archive.zip', json=[first]).status_code == 403
        headers = {'X-CSRF-Token': csrf}
        response = client.post('/api/stickers/archive.zip', json=[first, second], headers=headers)
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            assert len(zf.namelist()) == 2
        assert client.post('/api/stickers/archive.zip', json=[first, 999999], headers=headers).status_code == 404
        assert client.get('/reports/stickers?since=bad').status_code == 400
        assert client.get('/reports/stickers?since=2026-10-23&until=2026-01-01').status_code == 400
        assert client.get('/reports/stickers?page=0').status_code == 422
        assert client.get('/reports/stickers?state=saved&q=order-2').text.count('class="sticker-select"') == 1
        assert client.post('/api/stickers/1/retry').status_code == 403


def test_stickers_have_separate_permission(account):
    db.execute('INSERT INTO users(login,password_hash,role,sections,active,created_at) VALUES(?,?,?,?,1,?)',
               ('archive-reader', security.hash_password('reader-password'), access.PACKER, '["stickers"]', db.now_iso()))
    with TestClient(app, follow_redirects=False) as client:
        sign_in(client, 'archive-reader', 'reader-password')
        assert client.get('/reports/stickers').status_code == 200
        assert client.get('/reports').status_code == 403
        assert client.get('/logs').status_code == 403
        db.execute('UPDATE users SET sections = ? WHERE login = ?', ('["reports"]', 'archive-reader'))
        assert client.get('/reports/stickers').status_code == 403
        assert client.post('/api/stickers/archive.zip', json=[]).status_code == 403
        assert client.get('/api/stickers/1.pdf').status_code == 403


def test_retry_requires_enabled_worker_and_csrf(account, sample_data, monkeypatch):
    order = archive.search()[0][0]['id']
    with TestClient(app, follow_redirects=False) as client:
        csrf = sign_in(client)
        headers = {'X-CSRF-Token': csrf}
        assert client.post(f'/api/stickers/{order}/retry', headers=headers).status_code == 409
        monkeypatch.setattr(settings, 'sync_enabled', True)
        assert client.post(f'/api/stickers/{order}/retry', headers=headers).status_code == 200


def test_zip_size_limit_is_checked_before_loading(account, monkeypatch):
    saved = archive.save(queued(account), pdf())
    monkeypatch.setattr(archive, 'MAX_ZIP_BYTES', 1)
    with TestClient(app, follow_redirects=False) as client:
        csrf = sign_in(client)
        assert client.post('/api/stickers/archive.zip', json=[saved], headers={'X-CSRF-Token': csrf}).status_code == 400


def test_account_delete_removes_archive(account, avito_account):
    archive.save(queued(avito_account), pdf())
    accounts.delete(avito_account['id'])
    assert archive.search()[1] == 0
    assert not db.query('SELECT * FROM sticker_versions')


def test_regular_download_also_archives_before_packing(account, sample_data):
    market = registry.get(account['marketplace'])
    key = market.labels.pending(account['id'])[0]
    body = pdf(2)
    archive.remember_batch(account, [key], body)
    items, _ = archive.search(q=key)
    assert archive.version(items[0]['version_id'])['pdf'] == body


def test_ambiguous_batch_is_fetched_per_order(account, sample_data, monkeypatch):
    keys = registry.get('ozon').labels.pending(account['id'])[:2]
    assert len(keys) == 2
    seen = []

    def capture(shop, archive_id, key):
        seen.append(key)
        archive.save(archive_id, pdf(2))

    monkeypatch.setattr(archive, 'capture', capture)
    archive.remember_batch(account, keys, pdf(3))
    assert seen == keys
    for key in keys:
        item = archive.search(q=key)[0][0]
        assert len(PdfReader(io.BytesIO(archive.version(item['version_id'])['pdf'])).pages) == 2


def test_background_worker_saves_and_stops(sample_data, monkeypatch):
    import threading

    completed = threading.Event()
    original = archive.run_once

    def run():
        original()
        completed.set()

    monkeypatch.setattr(archive, 'run_once', run)
    worker = archive.ArchiveWorker()
    monkeypatch.setattr(archive, '_worker', worker)
    worker.start()
    try:
        assert completed.wait(5), 'фоновая загрузка не завершилась'
        assert db.query('SELECT id FROM sticker_versions')
        # enqueue не будит сам себя внутри фонового прохода.
        assert not worker.wake.is_set()
    finally:
        archive.stop_worker()
        worker.join(timeout=5)
    assert not worker.is_alive()


def test_date_filter_uses_the_same_local_day_as_the_page(account, monkeypatch):
    monkeypatch.setattr(settings, 'timezone_offset', 3)
    saved = archive.save(queued(account), pdf())
    db.execute('UPDATE sticker_versions SET saved_at = ? WHERE id = ?', ('2026-10-22T22:30:00+00:00', saved))
    assert archive.search(since='2026-10-23', until='2026-10-23')[1] == 1
    assert archive.search(until='2026-10-22')[1] == 0
