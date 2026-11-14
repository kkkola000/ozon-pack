"""ЧЗ обязан сохраниться точно, попасть в нужный заказ и быть принят площадкой."""
from __future__ import annotations

import io
import json
from copy import deepcopy
from contextvars import Context

import httpx
import pytest
import zxingcpp
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.core import db, labels, marking, marking_codes, order_lock, packing, printers
from app.main import app
from app.markets.ozon import client as ozon_client, store as ozon_store
from app.markets.yandex import client as yandex_client, store as yandex_store
from app.markets.avito import client as avito_client, store as avito_store

GTIN = '09506000134352'
BARCODE = GTIN[1:]


def code(serial='ABCDEF1234567', tail='abcdefghijklmnop'):
    return f'01{GTIN}21{serial}\x1d91ABCD\x1d92{tail}'


def marked_ozon(account, quantity=1, status='awaiting_packaging'):
    api = ozon_client.get_client(account)
    raw = deepcopy(next(iter(api._postings.values())))
    raw['status'] = status
    raw['substatus'] = 'posting_created'
    raw['products'] = [dict(raw['products'][0], quantity=quantity)]
    sku = str(raw['products'][0]['sku'])
    raw['requirements']['products_requiring_mandatory_mark'] = [sku]
    api._postings = {raw['posting_number']: raw}
    with db.write() as conn:
        conn.execute('DELETE FROM posting_items WHERE account_id=?', (account['id'],))
        conn.execute('DELETE FROM postings WHERE account_id=?', (account['id'],))
        ozon_store.upsert_posting(conn, account['id'], raw)
        conn.execute('INSERT OR REPLACE INTO products(account_id,sku,offer_id,name,barcodes) VALUES(?,?,?,?,?)',
                     (account['id'], sku, str(raw['products'][0]['offer_id']), 'Товар', json.dumps([BARCODE])))
        conn.execute('INSERT OR REPLACE INTO product_barcodes VALUES(?,?,?)', (account['id'], BARCODE, sku))
    labels.note_deliver(account)
    labels.mark_saved('postings', account['id'], [raw['posting_number']], 'posting_number')
    return api, raw['posting_number'], sku


def marked_yandex(account, quantity=1):
    api = yandex_client.get_client(account)
    raw = deepcopy(next(iter(api._orders.values())))
    raw.update(status='PROCESSING', substatus='STARTED', programType='FBS')
    raw['items'] = [dict(raw['items'][0], count=quantity, requiredInstanceTypes=['CIS'])]
    item = raw['items'][0]
    api._orders = {str(raw['orderId']): raw}
    with db.write() as conn:
        conn.execute('DELETE FROM yandex_order_items WHERE account_id=?', (account['id'],))
        conn.execute('DELETE FROM yandex_orders WHERE account_id=?', (account['id'],))
        yandex_store.upsert_yandex_order(conn, account['id'], raw)
        conn.execute('INSERT OR REPLACE INTO products(account_id,sku,offer_id,name,barcodes) VALUES(?,?,?,?,?)',
                     (account['id'], item['offerId'], item['offerId'], item['offerName'], json.dumps([BARCODE])))
        conn.execute('INSERT OR REPLACE INTO product_barcodes VALUES(?,?,?)', (account['id'], BARCODE, item['offerId']))
    return api, str(raw['orderId']), str(item['id'])


def scan(account, user, value):
    return packing.scan([account], user, value)


def finish_job(account, number):
    marking.process(account, number, force=True)
    marking.process(account, number, force=True)


def test_parser_and_generated_image_keep_gs_crypto_and_fnc1():
    raw = code('AA\\"BBcc12345', 'a+/0123456789XYZ=')
    parsed = marking_codes.parse(']d2' + raw + '\r\n')
    assert parsed.raw == raw
    image = marking_codes.image(parsed)
    decoded = zxingcpp.read_barcode(image)
    assert decoded.content_type == zxingcpp.ContentType.GS1
    assert decoded.bytes == raw.encode()
    document = PdfReader(io.BytesIO(marking_codes.pdf([parsed])))
    # Проверяем не только вход генератора, а изображение из конечного PDF.
    printed_image = list(document.pages[0].images)[0].image
    assert zxingcpp.read_barcode(printed_image).bytes == raw.encode()


@pytest.mark.parametrize('raw', [f'01{GTIN}21ABCDEF1234567', f'01{GTIN}21ABCDEF1234567\\u001d93AAAA',
                               code().replace(GTIN, GTIN[:-1]+'9'), code()+'\x00'])
def test_damaged_codes_are_not_accepted(raw):
    with pytest.raises(ValueError):
        marking_codes.parse(raw)


@pytest.mark.parametrize('status', ['awaiting_packaging', 'awaiting_deliver'])
def test_ozon_full_flow_preserves_code_and_confirms_remote_state(account, user, status):
    api, number, sku = marked_ozon(account, status=status)
    db.kv_set(marking.KV_AUTO, '1')
    opened = scan(account, user, BARCODE)
    assert opened['state']['active']
    assert opened['state']['items'][0]['marking']['status'] == 'waiting'
    assert opened['state']['sticker_download']['ready'] is (status == 'awaiting_deliver')
    assert opened['state']['marking']['pending_key'] == sku
    if status == 'awaiting_packaging':
        assert api._postings[number]['status'] == status
    result = scan(account, user, code())
    assert result['action'] == 'marking_saved'
    assert result['marking_print']['kind'] == 'marking:duplicate'
    saved = marking.order(account['id'], number)
    row = db.query_one('SELECT * FROM marking_codes')
    assert row['code'] != code() and code() not in row['code']
    assert marking.codes(saved, full=True)[0]['raw'] == code()
    assert result['state']['complete'] is False
    blocked = packing.complete(user)
    assert blocked['status'] == 'error'
    assert db.query_one('SELECT local_state FROM postings WHERE posting_number=?', (number,))['local_state'] != 'packed'
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'
    assert api._postings[number]['status'] == 'awaiting_deliver'
    assert api.marking_requests[0]['products'][0]['exemplars'][0]['marks'][0] == {'mark': code(), 'mark_type': 'mandatory_mark'}
    ready = packing.state(user)
    assert ready['items'][0]['marking']['status'] == 'accepted'
    assert ready['complete'] is True
    assert ready['sticker_download']['ready'] is True
    assert scan(account, user, number)['action'] == 'completed'


def test_each_unit_requires_barcode_then_unique_mark(account, user):
    api, number, sku = marked_ozon(account, quantity=2)
    assert scan(account, user, code())['status'] == 'error'
    scan(account, user, BARCODE)
    assert scan(account, user, BARCODE)['action'] == 'marking_required'
    first = scan(account, user, code())
    assert first['state']['marking']['status'] == 'collecting'
    duplicate = scan(account, user, code())
    assert 'marking_print' not in duplicate
    assert len(db.query('SELECT * FROM marking_codes')) == 1
    scan(account, user, BARCODE)
    scan(account, user, code('ABCDEF1234568'))
    finish_job(account, number)
    payload = api.marking_requests[-1]
    assert len(payload['products']) == 1
    assert len(payload['products'][0]['exemplars']) == 2
    assert {m['mark'] for e in payload['products'][0]['exemplars'] for m in e['marks']} == {code(), code('ABCDEF1234568')}


def test_print_disabled_does_not_disable_storage_or_transfer(yandex_account, user):
    api, number, key = marked_yandex(yandex_account)
    db.kv_set(marking.KV_AUTO, '0')
    opened = scan(yandex_account, user, BARCODE)
    assert opened['state']['sticker_download']['ready'] is False
    result = scan(yandex_account, user, code())
    assert 'marking_print' not in result
    assert api._orders[number]['substatus'] == 'STARTED'
    finish_job(yandex_account, number)
    saved = marking.order(yandex_account['id'], number)
    assert saved['status'] == 'accepted'
    assert api._orders[number]['substatus'] == 'READY_TO_SHIP'
    assert packing.state(user)['sticker_download']['ready'] is True
    assert api._orders[number]['items'][0]['instances'][0]['cisFull'] == code()
    assert marking.archive_details(saved['archive_id'])['items'][0]['item_key'] == key
    assert scan(yandex_account, user, number)['action'] == 'completed'


def test_ozon_http_200_is_not_marking_acceptance(account, user, monkeypatch):
    api, number, sku = marked_ozon(account)
    scan(account, user, BARCODE)
    scan(account, user, code())
    monkeypatch.setattr(api, 'exemplars_status', lambda *_: {'posting_number': number, 'status': 'PROCESSING_COMPLETED', 'products': []})
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] != 'accepted'
    assert api._postings[number]['status'] == 'awaiting_packaging'
    assert packing.state(user)['items'][0]['marking']['status'] == 'waiting'
    assert packing.complete(user)['status'] == 'error'


def test_yandex_invalid_is_red_and_cannot_complete(yandex_account, user, monkeypatch):
    api, number, key = marked_yandex(yandex_account)
    scan(yandex_account, user, BARCODE)
    scan(yandex_account, user, code())
    marking.process(yandex_account, number, force=True)
    monkeypatch.setattr(api, 'identifiers_status', lambda *_: {'status': 'OK', 'result': {'items': [
        {'id': int(key), 'cis': [{'value': code(), 'status': 'INVALID', 'substatus': 'WRONG_OWNER_INN'}]}]}})
    marking.process(yandex_account, number, force=True)
    assert marking.order(yandex_account['id'], number)['status'] == 'error'
    assert packing.state(user)['items'][0]['marking']['status'] == 'error'
    assert packing.complete(user)['status'] == 'error'
    assert api._orders[number]['substatus'] == 'STARTED'


def test_timed_out_write_is_polled_not_blindly_repeated(account, user, monkeypatch):
    api, number, _sku = marked_ozon(account)
    scan(account, user, BARCODE)
    scan(account, user, code())
    actual = api.exemplars_set
    writes = []
    def uncertain(payload):
        writes.append(payload)
        actual(payload)
        raise ozon_client.OzonError('timeout')
    monkeypatch.setattr(api, 'exemplars_set', uncertain)
    marking.process(account, number, force=True)
    assert marking.order(account['id'], number)['status'] == 'uncertain'
    marking.process(account, number, force=True)
    assert len(writes) == 1
    assert marking.order(account['id'], number)['status'] == 'accepted'


def test_archive_and_pdf_require_permissions_and_keep_single_sticker(account, user):
    api, number, sku = marked_ozon(account)
    scan(account, user, BARCODE)
    scan(account, user, code())
    saved = marking.order(account['id'], number)
    mark_id = marking.codes(saved)[0]['id']
    with TestClient(app, follow_redirects=False) as client:
        assert client.get(f'/api/pack/marking/{mark_id}.pdf').status_code == 401
        assert client.get(f'/api/stickers/marking/{mark_id}.pdf').status_code == 401
        client.post('/login', data={'login': 'admin', 'password': 'test-admin-pass'})
        assert client.get(f'/api/pack/marking/{mark_id}.pdf').status_code == 200
        detail = client.get(f'/api/stickers/{saved["archive_id"]}/marking')
        assert detail.status_code == 200
        assert detail.json()['items'][0]['serial'] == 'ABCDEF1234567'
        assert 'code' not in detail.json()['items'][0] and 'raw' not in detail.json()['items'][0]
        for url in (f'/api/stickers/marking/{mark_id}.pdf', f'/api/stickers/{saved["archive_id"]}/marking.pdf'):
            response = client.get(url)
            assert response.status_code == 200, response.text
            assert response.headers['content-type'] == 'application/pdf'
            assert response.content.startswith(b'%PDF-')
        db.execute('UPDATE marking_codes SET active=0, status=? WHERE id=?', ('removed', mark_id))
        removed = client.get(f'/api/stickers/marking/{mark_id}.pdf')
        assert removed.status_code == 404 and 'не найден' in removed.json()['detail']
        db.execute('UPDATE marking_codes SET active=1, status=? WHERE id=?', ('saved', mark_id))
    assert db.count('SELECT COUNT(*) FROM marking_codes') == 1
    assert marking.codes(saved, full=True)[0]['raw'] == code()


def test_one_common_printer_for_all_markets():
    docs = [d for d in printers.documents() if d['kind'] == 'marking:duplicate']
    assert len(docs) == 1
    with pytest.raises(ValueError, match='один общий'):
        printers.save([{'kind': 'marking:duplicate', 'size': '58x40', 'printer': 'A'},
                       {'kind': 'marking:duplicate', 'size': '100x150', 'printer': 'B'}])


def test_failure_before_write_keeps_task_retryable(account, user, monkeypatch):
    api, number, _sku = marked_ozon(account)
    scan(account, user, BARCODE)
    scan(account, user, code())
    original = api.exemplars_validate
    with monkeypatch.context() as patch:
        patch.setattr(api, 'exemplars_validate', lambda *_: (_ for _ in ()).throw(ozon_client.OzonError('timeout')))
        marking.process(account, number, force=True)
    assert marking.order(account['id'], number)['status'] == 'queued'
    assert api.marking_requests == []
    assert api.exemplars_validate == original
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'
    assert len(api.marking_requests) == 1


def test_definite_rejection_can_be_retried_without_duplicate_print(account, user, monkeypatch):
    api, number, _sku = marked_ozon(account)
    db.kv_set(marking.KV_AUTO, '1')
    scan(account, user, BARCODE)
    scan(account, user, code())
    with monkeypatch.context() as patch:
        patch.setattr(api, 'exemplars_set', lambda *_: (_ for _ in ()).throw(ozon_client.OzonError('bad', status=422)))
        marking.process(account, number, force=True)
    assert marking.order(account['id'], number)['status'] == 'error'
    result = scan(account, user, code())
    assert 'marking_print' not in result
    assert marking.order(account['id'], number)['status'] == 'queued'
    assert db.count('SELECT COUNT(*) FROM marking_codes') == 1
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'


def test_rejection_of_second_unit_keeps_first_unit_bound(account, user, monkeypatch):
    api, number, _sku = marked_ozon(account, quantity=2)
    for serial in ('ABCDEF1234567', 'ABCDEF1234568'):
        scan(account, user, BARCODE)
        scan(account, user, code(serial))
    original = api.exemplars_validate
    def reject_second(*args):
        result = original(*args)
        result['products'][0]['valid'] = False
        result['products'][0]['exemplars'][1]['valid'] = False
        return result
    with monkeypatch.context() as patch:
        patch.setattr(api, 'exemplars_validate', reject_second)
        marking.process(account, number, force=True)
    saved = marking.order(account['id'], number)
    first, second = marking.codes(saved)
    assert first['status'] == 'saved' and second['status'] == 'error'
    result = scan(account, user, code('ABCDEF1234569'))
    assert result['status'] == 'ok'
    active = marking.codes(saved, full=True)
    assert active[0]['id'] == first['id']
    assert active[1]['raw'] == code('ABCDEF1234569')
    assert db.query_one('SELECT active FROM marking_codes WHERE id=?', (second['id'],))['active'] == 0
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'


def test_one_mark_cannot_be_used_in_another_market(account, yandex_account, user):
    api, number, _sku = marked_ozon(account)
    scan(account, user, BARCODE)
    scan(account, user, code())
    packing.release(user)
    marked_yandex(yandex_account)
    scan(yandex_account, user, BARCODE)
    rejected = scan(yandex_account, user, code())
    assert rejected['status'] == 'error'
    assert 'повторно' in rejected['message']
    assert db.count('SELECT COUNT(*) FROM marking_codes') == 1


def test_wrong_gtin_is_not_stored(account, user):
    marked_ozon(account)
    scan(account, user, BARCODE)
    wrong = code().replace(GTIN, '04612345678903')  # другой GTIN с корректной контрольной цифрой
    assert marking_codes.parse(wrong).gtin != GTIN
    rejected = scan(account, user, wrong)
    assert rejected['status'] == 'error' and 'GTIN' in rejected['message']
    assert db.count('SELECT COUNT(*) FROM marking_codes') == 0


def test_stale_packer_cannot_attach_mark_or_release_new_owner(account, user, other_user):
    api, number, _sku = marked_ozon(account)
    scan(account, user, BARCODE)
    db.execute('UPDATE postings SET claim_user_id=?,claim_login=?,claim_at=? WHERE account_id=? AND posting_number=?',
               (other_user['id'], other_user['login'], db.now_iso(), account['id'], number))
    result = scan(account, user, code())
    assert result['status'] == 'error'
    assert db.count('SELECT COUNT(*) FROM marking_codes') == 0
    packing.release(user)
    assert db.query_one('SELECT claim_user_id FROM postings WHERE account_id=? AND posting_number=?',
                        (account['id'], number))['claim_user_id'] == other_user['id']


def test_relaxed_requirement_after_submission_keeps_original_obligation(account, user):
    api, number, _sku = marked_ozon(account)
    scan(account, user, BARCODE)
    scan(account, user, code())
    marking.process(account, number, force=True)
    api._postings[number]['requirements']['products_requiring_mandatory_mark'] = []
    marking.process(account, number, force=True)
    assert marking.order(account['id'], number)['status'] == 'accepted'
    assert marking.has_required(marking.order(account['id'], number))


def test_remote_composition_change_never_becomes_green(account, user):
    api, number, _sku = marked_ozon(account)
    scan(account, user, BARCODE)
    scan(account, user, code())
    api._postings[number]['products'][0]['quantity'] = 2
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'error'
    assert packing.state(user)['items'][0]['marking']['status'] == 'error'
    assert api._postings[number]['status'] == 'awaiting_packaging'


def test_lease_is_reentrant_but_excludes_another_context(account):
    with order_lock.hold(account['id'], 'order'):
        with order_lock.hold(account['id'], 'order'):
            assert db.count('SELECT COUNT(*) FROM order_operations') == 1
        with pytest.raises(ValueError, match='обрабатывается'):
            Context().run(lambda: order_lock.hold(account['id'], 'order').__enter__())
    assert db.count('SELECT COUNT(*) FROM order_operations') == 0
    db.execute('INSERT INTO order_operations VALUES(?,?,?,?)', (account['id'], 'order', 'dead', '2000-01-01'))
    with order_lock.hold(account['id'], 'order'):
        assert db.query_one('SELECT token FROM order_operations')['token'] != 'dead'


@pytest.mark.parametrize('vendor', ['ozon', 'yandex'])
def test_wire_json_escapes_gs_exactly_once_and_accepts_empty_200(vendor):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200)
    if vendor == 'ozon':
        api = ozon_client.OzonClient('client', 'key')
        api._client.close()
        api._client = httpx.Client(base_url='https://api-seller.ozon.ru', transport=httpx.MockTransport(respond))
        api.exemplars_set({'posting_number': '123', 'products': [{'mark': code()}]})
    else:
        api = yandex_client.YandexClient('9000001', 'token')
        api._client.close()
        api._client = httpx.Client(base_url='https://api.partner.market.yandex.ru', transport=httpx.MockTransport(respond))
        api.order_boxes('123', '456', [{'items': [{'id': 1, 'fullCount': 1, 'instances': [{'cis': code()}]}]}])
    try:
        assert len(requests) == 1
        wire = requests[0].content
        assert b'\\u001d' in wire and b'\\\\u001d' not in wire and b'\x1d' not in wire
        assert code() in str(json.loads(wire)).replace('\\x1d', '\x1d')
    finally:
        api.close()


@pytest.mark.parametrize('via', ['label', 'product'])
def test_avito_scan_confirms_remote_ready_status(avito_account, user, via):
    api = avito_client.get_client(avito_account)
    raw = deepcopy(next(o for o in api._orders.values() if o['status'] == 'on_confirmation'))
    raw['items'] = [dict(raw['items'][0], count=2)]
    api._orders = {raw['id']: raw}
    with db.write() as conn:
        avito_store.upsert_avito_order(conn, avito_account['id'], raw)
        sku = str(raw['items'][0]['avitoId'])
        conn.execute('INSERT INTO products(account_id,sku,name,barcodes) VALUES(?,?,?,?)',
                     (avito_account['id'], sku, 'Товар', json.dumps([BARCODE])))
        conn.execute('INSERT INTO product_barcodes VALUES(?,?,?)', (avito_account['id'], BARCODE, sku))
    result = scan(avito_account, user, raw['marketplaceId'] if via == 'label' else BARCODE)
    assert result['status'] == 'ok', result['message']
    assert api._orders[raw['id']]['status'] == 'ready_to_ship'
    assert result['state']['active']['status'] == 'ready_to_ship'
    assert result['state']['done'] == (1 if via == 'product' else 0)


def test_yandex_full_layout_preserves_other_unit_attributes():
    from app.markets.yandex.marking import _boxes
    raw = {'items': [{'id': 1, 'count': 2, 'instances': [{'rnpt': 'first'}, {'rnpt': 'second'}]}],
           'delivery': {'boxesLayout': [{'items': [{'id': 1, 'fullCount': 1}]},
                                        {'items': [{'id': 1, 'fullCount': 1}]}]}}
    result = _boxes(raw, [{'item_key': '1', 'unit_no': 1, 'raw': code()},
                          {'item_key': '1', 'unit_no': 2, 'raw': code('ABCDEF1234568')}])
    assert [b['items'][0]['instances'][0]['rnpt'] for b in result] == ['first', 'second']
    assert result[1]['items'][0]['instances'][0]['cis'] == code('ABCDEF1234568')


@pytest.mark.parametrize('vendor', ['ozon', 'yandex', 'avito'])
def test_http_200_without_real_ready_status_does_not_open_order(account, yandex_account, avito_account, user, monkeypatch, vendor):
    if vendor == 'ozon':
        api, number, _sku = marked_ozon(account)
        api._postings[number]['requirements']['products_requiring_mandatory_mark'] = []
        with db.write() as conn:
            ozon_store.upsert_posting(conn, account['id'], api._postings[number])
        monkeypatch.setattr(api, 'ship', lambda *_: {'postings': [number]})
        shop = account
    elif vendor == 'yandex':
        api, number, _key = marked_yandex(yandex_account)
        api._orders[number]['items'][0]['requiredInstanceTypes'] = []
        monkeypatch.setattr(api, 'ready_to_ship', lambda *_: {'status': 'OK'})
        shop = yandex_account
    else:
        shop = avito_account
        api = avito_client.get_client(shop)
        raw = next(o for o in api._orders.values() if o['status'] == 'on_confirmation')
        number = raw['id']
        with db.write() as conn:
            avito_store.upsert_avito_order(conn, shop['id'], raw)
        monkeypatch.setattr(api, 'confirm_once', lambda *_: None)
    result = scan(shop, user, number)
    assert result['status'] == 'error'
    assert result['state']['active'] is None
    assert db.query_one('SELECT status FROM ready_requests WHERE account_id=? AND order_id=?',
                        (shop['id'], number))['status'] == 'uncertain'


def test_ozon_ship_failed_permits_confirmed_retry(account, user, monkeypatch):
    api, number, _sku = marked_ozon(account)
    api._postings[number]['requirements']['products_requiring_mandatory_mark'] = []
    with db.write() as conn:
        ozon_store.upsert_posting(conn, account['id'], api._postings[number])
    with monkeypatch.context() as patch:
        patch.setattr(api, 'ship', lambda *_: {'postings': [number]})
        assert scan(account, user, number)['status'] == 'error'
    api._postings[number]['substatus'] = 'ship_failed'
    result = scan(account, user, number)
    assert result['status'] == 'ok'
    assert api._postings[number]['status'] == 'awaiting_deliver'


def test_codes_survive_connection_restart_and_worker_finishes_job(account, user):
    api, number, _sku = marked_ozon(account)
    scan(account, user, BARCODE)
    scan(account, user, code())
    db._local.conn.close()
    db._local.conn = None
    marking.run_once()
    assert marking.order(account['id'], number)['status'] == 'checking'
    db.execute('UPDATE marking_orders SET checked_at=NULL')
    marking.run_once()
    assert marking.order(account['id'], number)['status'] == 'accepted'
    assert marking.codes(marking.order(account['id'], number), full=True)[0]['raw'] == code()


def test_marking_settings_keep_other_printers_and_require_csrf_and_manager(other_user):
    import re

    printers.save([{'kind': 'ozon:label', 'size': '58x40', 'printer': 'OLD', 'gap': 1,
                    'shift': {'left': 2}}])
    original = next(r for r in printers.rows() if r['kind'] == 'ozon:label')
    with TestClient(app, follow_redirects=False) as client:
        client.post('/login', data={'login': 'admin', 'password': 'test-admin-pass'})
        payload = {'enabled': True, 'printer': 'CHZ ONLY', 'size': '58x40'}
        assert client.post('/api/settings/marking', json=payload).status_code == 403
        token = re.search(r'name="csrf-token" content="([^"]*)"', client.get('/settings').text).group(1)
        client.headers['X-CSRF-Token'] = token
        assert client.post('/api/settings/marking', json=dict(payload, enabled='false')).status_code == 400
        assert client.post('/api/settings/marking', json=payload).status_code == 200
        assert marking.enabled()
        assert next(r for r in printers.rows() if r['kind'] == 'marking:duplicate')['printer'] == 'CHZ ONLY'
        assert next(r for r in printers.rows() if r['kind'] == 'ozon:label') == original
    with TestClient(app, follow_redirects=False) as client:
        client.post('/login', data={'login': other_user['login'], 'password': 'secret123'})
        assert client.post('/api/settings/marking', json=payload).status_code == 403


def test_failed_validation_is_retryable_but_never_green(account, user, monkeypatch):
    api, number, _sku = marked_ozon(account)
    scan(account, user, BARCODE)
    scan(account, user, code())
    marking.process(account, number, force=True)
    original = api.exemplars_status
    def failed(order_id):
        result = original(order_id)
        result['products'][0]['exemplars'][0]['marks'][0]['check_status'] = 'failed'
        return result
    with monkeypatch.context() as patch:
        patch.setattr(api, 'exemplars_status', failed)
        marking.process(account, number, force=True)
    assert marking.order(account['id'], number)['status'] == 'queued'
    assert packing.state(user)['items'][0]['marking']['status'] == 'waiting'
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'


@pytest.mark.parametrize('status', [401, 429])
def test_refused_poll_does_not_requeue_previous_write(account, user, monkeypatch, status):
    api, number, _sku = marked_ozon(account)
    scan(account, user, BARCODE)
    scan(account, user, code())
    marking.process(account, number, force=True)
    with monkeypatch.context() as patch:
        patch.setattr(api, 'exemplars_status', lambda *_: (_ for _ in ()).throw(ozon_client.OzonError('later', status=status)))
        marking.process(account, number, force=True)
    assert marking.order(account['id'], number)['status'] == 'checking'
    assert len(api.marking_requests) == 1

    marking.process(account, number, force=True)
    assert marking.order(account['id'], number)['status'] == 'accepted'
    assert len(api.marking_requests) == 1


def test_market_error_does_not_put_full_mark_into_logs(account, user, monkeypatch, caplog):
    api, number, _sku = marked_ozon(account)
    scan(account, user, BARCODE)
    scan(account, user, code())
    monkeypatch.setattr(api, 'ship', lambda *_: (_ for _ in ()).throw(ozon_client.OzonError(code(), status=400)))
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] != 'accepted'
    assert code() not in caplog.text
    for row in db.query('SELECT message,barcode FROM events'):
        assert code() not in str(dict(row))
