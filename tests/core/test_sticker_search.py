"""Find shipment stickers by their active marking bindings, without API calls."""
from __future__ import annotations

import hashlib
import json
import re

import pytest
from fastapi.testclient import TestClient

from app.core import access, db, marking, marking_codes, security, sticker_archive as archive
from app.main import app
from tests.core.test_marking import GTIN, code
from tests.core.test_sticker_archive import pdf, queued, sign_in


def add_mark(account, number, raw, *, archive_id=None, active=True):
    parsed = marking_codes.parse(raw)
    if archive_id is None:
        archive_id = queued(account, number)
        archive.save(archive_id, pdf())
        db.execute('INSERT INTO marking_orders(account_id,order_id,archive_id,products,context,created_at) '
                   'VALUES(?,?,?,?,?,?)',
                   (account['id'], number, archive_id, json.dumps([]), marking._seal('{}'), db.now_iso()))
    saved = marking.order(account['id'], number)
    mark_id = db.execute(
        'INSERT INTO marking_codes(marking_order_id,item_key,unit_no,fingerprint,code,gtin,serial,active,created_at,created_by) '
        'VALUES(?,?,?,?,?,?,?,?,?,?)',
        (saved['id'], 'sku', len(marking.codes(saved, active=False)) + 1,
         hashlib.sha256((parsed.gtin + '\0' + parsed.serial).encode('ascii')).hexdigest(),
         marking._seal(parsed.raw), parsed.gtin, parsed.serial, int(active), db.now_iso(), 'admin'),
    ).lastrowid
    return archive_id, mark_id


@pytest.fixture
def marked_archive(account, yandex_account):
    first, mark_id = add_mark(account, 'ozon-first', code())
    add_mark(account, 'ozon-first', code('ABCDEF1234568'), archive_id=first)
    second, _ = add_mark(account, 'ozon-second', code('ABCDEF1234569'))
    third, _ = add_mark(yandex_account, 'yandex-third', code('ABCDEF1234570'))
    add_mark(account, 'other-gtin', code('ABCDEF1234571').replace(GTIN, '04612345678903'))
    return first, second, third, mark_id


@pytest.mark.parametrize('query', [GTIN, GTIN[1:], '  ' + GTIN + '  '])
def test_gtin_finds_all_shipments_once(query, marked_archive):
    first, second, third, _ = marked_archive
    items, total = archive.search(q=query, state='saved')
    assert total == 3
    assert {item['id'] for item in items} == {first, second, third}
    assert next(item for item in items if item['id'] == first)['marking_count'] == 2


def test_gtin_respects_account_market_and_dates(account, marked_archive):
    first, second, third, _ = marked_archive
    assert {r['id'] for r in archive.search(q=GTIN, account_id=account['id'])[0]} == {first, second}
    assert [r['id'] for r in archive.search(q=GTIN, market='yandex')[0]] == [third]
    db.execute('UPDATE sticker_versions SET saved_at=? WHERE archive_id=?', (db.now_iso()[:10] + 'T12:00:00+00:00', first))
    assert archive.search(q=GTIN, until='2000-01-01')[1] == 0
    assert archive.search(q=GTIN, since=db.now_iso()[:10])[1] == 3


@pytest.mark.parametrize('variant', [
    lambda raw: raw,
    lambda raw: raw.replace('\x1d', '<GS>'),
    lambda raw: raw.replace('\x1d', r'\<gs>'),
    lambda raw: ']d2' + raw,
    lambda raw: ']d8\x1d' + raw + '\r\n',
    lambda raw: ' \t' + raw,
])
def test_full_code_finds_only_its_exact_binding(variant, marked_archive):
    first, _, _, _ = marked_archive
    rows, total = archive.search(q=variant(code()), state='saved')
    assert total == 1 and rows[0]['id'] == first


@pytest.mark.parametrize('query', [
    code('abcdef1234567'),
    code(tail='different-crypto-tail'),
    code().replace('91ABCD', '91ABCE'),
    code().split('\x1d')[0],
    code() + "' OR 1=1 --",
])
def test_full_code_does_not_fall_back_to_gtin(query, marked_archive):
    assert archive.search(q=query, state='saved')[1] == 0


def test_full_code_decrypts_only_its_candidate(marked_archive, monkeypatch):
    opened = []
    original = marking._open

    def decrypt(value):
        opened.append(value)
        return original(value)

    monkeypatch.setattr(marking, '_open', decrypt)
    assert archive.search(q=GTIN)[1] == 3
    assert not opened
    assert archive.search(q=code())[1] == 1
    assert len(opened) == 1


@pytest.mark.parametrize('profile', ['compact-93', 'joined-91-92', 'space-in-crypto'])
def test_full_code_search_uses_the_same_parser_as_assembly(account, profile):
    if profile == 'compact-93':
        stored = f'01{GTIN}21ABCDEF123456793ABCD'
        query = stored[:31] + '\x1d' + stored[31:]
    elif profile == 'joined-91-92':
        query = f'01{GTIN}21ABCDEF123456791ABCD92' + 'a' * 43 + '='
        stored = marking_codes.parse(query).raw
    else:
        stored = query = code(tail='crypto-with-a-space ')
    archive_id, _ = add_mark(account, 'exact-payload', stored)
    assert archive.search(q=query)[0][0]['id'] == archive_id
    if profile == 'space-in-crypto':
        assert archive.search(q=query.rstrip())[1] == 0


def test_removed_and_expired_bindings_are_not_searchable(account, marked_archive):
    first, _, _, mark_id = marked_archive
    db.execute("UPDATE marking_codes SET active=0,status='removed' WHERE id=?", (mark_id,))
    assert archive.search(q=code())[1] == 0
    db.execute('UPDATE marking_codes SET active=0 WHERE marking_order_id IN '
               '(SELECT id FROM marking_orders WHERE archive_id=?)', (first,))
    assert first not in {r['id'] for r in archive.search(q=GTIN)[0]}
    _, last_mark = add_mark(account, 'expired', code('EXPIRED123456'))
    archived = db.query_one('SELECT m.archive_id FROM marking_orders m JOIN marking_codes c '
                            'ON c.marking_order_id=m.id WHERE c.id=?', (last_mark,))['archive_id']
    db.execute("UPDATE sticker_orders SET created_at='2000-01-01' WHERE id=?", (archived,))
    db.execute("UPDATE sticker_versions SET saved_at='2000-01-01' WHERE archive_id=?", (archived,))
    assert archive.search(q=code('EXPIRED123456'))[1] == 0


def test_archive_search_survives_removal_of_source_order(account, marked_archive):
    first, _, _, _ = marked_archive
    db.execute('DELETE FROM postings WHERE account_id=?', (account['id'],))
    assert archive.search(q=code())[0][0]['id'] == first
    assert archive.search(q=GTIN)[1] == 3


@pytest.mark.parametrize('prefix', ['', ' \t'])
def test_full_code_form_search_keeps_payload_out_of_urls(prefix, marked_archive):
    first, _, _, _ = marked_archive
    with TestClient(app, follow_redirects=False) as client:
        sign_in(client)
        response = client.post('/reports/stickers', data={'q': prefix + code(), 'since': '', 'until': ''})
        assert response.status_code == 200
        assert response.url.path == '/reports/stickers' and not response.url.query
        assert 'location' not in response.headers
        assert response.headers['cache-control'] == 'no-store'
        assert re.findall(r'data-archive-id="(\d+)"', response.text) == [str(first)]
        assert 'id="stickers-more"' not in response.text
        assert f'data-marking-archive="{first}"' in response.text
        assert 'method="post" action="/reports/stickers"' in response.text
        assert client.post('/reports/stickers', data={'q': code(), 'account_id': '-1'}).text.count('data-archive-id=') == 0
        assert client.post('/reports/stickers', data={'q': code(), 'since': 'bad'}).status_code == 400


@pytest.mark.parametrize('query', [GTIN, 'ii50082908916', 'II50082908916', '50082908916', 'ozon-first'])
def test_number_and_gtin_form_search_preserve_links_and_filters(query, account, marked_archive):
    first, _, _, _ = marked_archive
    db.execute('INSERT INTO sticker_aliases(archive_id,account_id,alias) VALUES(?,?,?)',
               (first, account['id'], 'ii50082908916'))
    with TestClient(app, follow_redirects=False) as client:
        sign_in(client)
        response = client.post('/reports/stickers', data={'q': query, 'account_id': account['id']})
        assert response.status_code == 303
        found = client.get(response.headers['location'])
        assert found.status_code == 200
        assert f'data-archive-id="{first}"' in found.text
        assert 'yandex-third' not in found.text


def test_search_endpoint_requires_the_stickers_section(account):
    with TestClient(app, follow_redirects=False) as client:
        assert client.post('/reports/stickers', data={'q': code()}).status_code == 303
        db.execute('INSERT INTO users(login,password_hash,role,sections,active,created_at) VALUES(?,?,?,?,1,?)',
                   ('only-assembly', security.hash_password('reader-password'), access.PACKER, '["pack"]', db.now_iso()))
        sign_in(client, 'only-assembly', 'reader-password')
        assert client.post('/reports/stickers', data={'q': code()}).status_code == 403


def test_gtin_search_pagination_has_no_duplicate_shipments(account):
    for i in range(35):
        add_mark(account, f'shipment-{i:02}', code(f'SERIAL{i:07}'))
    first, count = archive.search(q=GTIN, page=1)
    second, _ = archive.search(q=GTIN, page=2)
    assert count == 35 and len(first) == 30 and len(second) == 5
    assert not {r['id'] for r in first} & {r['id'] for r in second}
