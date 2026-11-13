"""Разделение подтверждается API; единицы, ЧЗ и ярлыки не теряются."""
import io
import json
import re
from copy import deepcopy
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.core import cargo, db, marking, packing
from app.core.config import settings
from app.main import app
from app.markets.ozon import cargo as ozon_cargo, store as ozon_store
from app.markets.yandex import client as yandex_client, store as yandex_store
from tests.core.test_marking import BARCODE, code, finish_job, marked_ozon, marked_yandex, scan


@pytest.fixture(autouse=True)
def splitting(monkeypatch):
    monkeypatch.setattr(settings, 'split_orders_on_pack', True)
    monkeypatch.setattr(settings, 'autoprint', False)


def ordinary_ozon(account, quantity=2):
    api, number, sku = marked_ozon(account, quantity=quantity)
    api._postings[number]['requirements']['products_requiring_mandatory_mark'] = []
    with db.write() as conn:
        ozon_store.upsert_posting(conn, account['id'], api._postings[number])
        conn.execute('UPDATE posting_items SET mandatory_mark=0 WHERE account_id=?', (account['id'],))
        conn.execute('UPDATE postings SET requires_mark=0 WHERE account_id=?', (account['id'],))
    return api, number, sku


def ordinary_yandex(account, quantity=2, *, ready=False):
    api, number, key = marked_yandex(account, quantity=quantity)
    raw = api._orders[number]
    raw['items'][0]['requiredInstanceTypes'] = []
    if ready:
        raw['substatus'] = 'READY_TO_SHIP'
    with db.write() as conn:
        yandex_store.upsert_yandex_order(conn, account['id'], raw)
    return api, number, key


@pytest.fixture
def client(user):
    with TestClient(app, follow_redirects=False) as client:
        client.post('/login', data={'login': 'admin', 'password': 'test-admin-pass'})
        csrf = re.search(r'name="csrf-token" content="([^"]*)"', client.get('/pack').text).group(1)
        client.headers['X-CSRF-Token'] = csrf
        yield client


def test_ozon_automatically_splits_and_opens_the_scanned_unit(account, user, monkeypatch):
    api, number, sku = ordinary_ozon(account)
    ship = Mock(wraps=api.ship)
    monkeypatch.setattr(api, 'ship', ship)
    result = scan(account, user, BARCODE)
    assert result['status'] == 'ok', result
    assert result['state']['total'] == 1 and result['state']['done'] == 1
    assert ship.call_args.args[1] == [[{'product_id': int(sku), 'quantity': 1}]] * 2
    children = ozon_cargo.children(account['id'], number)
    assert len(children) == 2 and all(c['verified'] for c in children)
    assert scan(account, user, result['state']['active']['posting_number'])['action'] == 'completed'
    again = scan(account, user, BARCODE)
    assert again['state']['active']['posting_number'] != result['state']['active']['posting_number']
    assert ship.call_count == 1


def test_ready_ozon_is_never_split_again(account, user, monkeypatch):
    api, number, _sku = ordinary_ozon(account)
    api._postings[number]['status'] = 'awaiting_deliver'
    monkeypatch.setattr(api, 'ship', Mock(side_effect=AssertionError('already ready')))
    result = scan(account, user, number)
    assert result['status'] == 'ok', result
    assert result['state']['total'] == 2
    assert not ozon_cargo.children(account['id'], number)


def test_ozon_split_preserves_both_full_codes_and_scanned_units(account, user):
    api, number, _sku = marked_ozon(account, quantity=2)
    scan(account, user, BARCODE)
    scan(account, user, code())
    scan(account, user, BARCODE)
    scan(account, user, code('ABCDEF1234568'))
    finish_job(account, number)
    rows = db.query('SELECT * FROM marking_orders WHERE status=\'accepted\'')
    assert len(rows) == 2, [dict(r) for r in db.query('SELECT order_id,status,error FROM marking_orders')]
    all_codes = [c for row in rows for c in marking.codes(dict(row), full=True)]
    assert {c['raw'] for c in all_codes} == {code(), code('ABCDEF1234568')}
    assert len({c['marking_order_id'] for c in all_codes}) == 2
    assert all(c['unit_no'] == 1 and c['active'] for c in all_codes)
    assert len(api.marking_requests) == 1
    state = packing.state(user)
    assert state['total'] == state['done'] == 1
    assert state['items'][0]['marking']['status'] == 'accepted'
    assert state['sticker_download']['ready']
    reports = db.query("SELECT posting_number,unit_no FROM shipped_items WHERE account_id=? AND status='ok'", (account['id'],))
    assert len(reports) == 2 and all(r['unit_no'] == 1 for r in reports)
    assert {r['posting_number'] for r in reports} == {r['order_id'] for r in rows}


def test_missing_child_code_never_gets_guessed_or_resubmitted(account, user, monkeypatch):
    api, number, _sku = marked_ozon(account, quantity=2)
    original = api.ship
    lost = {}

    def ship(*args):
        result = original(*args)
        child = result['postings'][1]
        lost['child'] = child
        lost['marks'] = deepcopy(api._marking_payloads[child]['products'][0]['exemplars'][0]['marks'])
        api._marking_payloads[child]['products'][0]['exemplars'][0]['marks'] = []
        api._apply_exemplars(child)
        return result

    spy = Mock(side_effect=ship)
    monkeypatch.setattr(api, 'ship', spy)
    scan(account, user, BARCODE)
    scan(account, user, code())
    scan(account, user, BARCODE)
    scan(account, user, code('ABCDEF1234568'))
    finish_job(account, number)
    assert cargo.split_pending(account['id'], number)
    assert not packing.state(user)['sticker_download']['ready']
    assert packing.complete(user)['status'] == 'error'
    assert marking.order(account['id'], number)['status'] != 'accepted'
    api._marking_payloads[lost['child']]['products'][0]['exemplars'][0]['marks'] = lost['marks']
    api._apply_exemplars(lost['child'])
    marking.process(account, number, force=True)
    assert not cargo.split_pending(account['id'], number)
    assert spy.call_count == 1


def test_yandex_auto_boxes_and_every_label_must_be_scanned(yandex_account, user):
    api, number, _key = ordinary_yandex(yandex_account)
    first = scan(yandex_account, user, BARCODE)
    assert first['status'] == 'ok', first
    assert len(first['state']['cargo']['boxes']) == 2
    assert api._orders[number]['substatus'] == 'READY_TO_SHIP'
    scan(yandex_account, user, BARCODE)
    assert scan(yandex_account, user, number)['status'] == 'error'
    labels = cargo.get(yandex_account['id'], number)['boxes']
    one = scan(yandex_account, user, labels[0]['barcode'])
    assert one['action'] == 'cargo_label_scanned'
    duplicate = scan(yandex_account, user, labels[0]['barcode'])
    assert duplicate['status'] == 'warning'
    assert len(cargo.get(yandex_account['id'], number)['checked']) == 1
    assert scan(yandex_account, user, labels[1]['barcode'])['action'] == 'completed'


def test_one_marked_item_two_boxes_has_one_code_and_one_product_scan(yandex_account, user):
    api, number, key = marked_yandex(yandex_account)
    scan(yandex_account, user, BARCODE)
    saved = cargo.open_editor(yandex_account, number, user)
    token = saved['units'][0]['id']
    cargo.save_editor(yandex_account, number, user, saved['revision'],
                      [{'id': 'box-1', 'units': [token]}, {'id': 'new-box', 'units': [token]}])
    scan(yandex_account, user, code())
    finish_job(yandex_account, number)
    state = packing.state(user)
    assert state['total'] == state['done'] == 1
    assert state['marking']['status'] == 'accepted', state
    assert len(db.query('SELECT * FROM marking_codes WHERE active=1')) == 1
    sent = api.box_requests[-1]
    assert [b['items'][0]['partialCount'] for b in sent] == [{'current': 1, 'total': 2}, {'current': 2, 'total': 2}]
    assert [b['items'][0]['instances'][0]['cis'] for b in sent] == [code(), code()]
    assert all(b['items'][0]['id'] == int(key) for b in sent)


def test_yandex_ready_rejection_keeps_the_old_layout(yandex_account, user):
    api, number, _key = ordinary_yandex(yandex_account, 1, ready=True)
    api.reject_ready_boxes = True
    scan(yandex_account, user, BARCODE)
    saved = cargo.open_editor(yandex_account, number, user)
    token = saved['units'][0]['id']
    with pytest.raises(yandex_client.YandexError):
        cargo.save_editor(yandex_account, number, user, saved['revision'],
                          [{'id': 'box-1', 'units': [token]}, {'id': 'new-box', 'units': [token]}])
    after = cargo.get(yandex_account['id'], number)
    assert after['status'] == 'baseline' and after['boxes'] == saved['boxes']
    assert after['revision'] == saved['revision']
    cargo.labels_allowed(yandex_account['id'], number)


def test_yandex_timeout_is_verified_without_a_second_write(yandex_account, user, monkeypatch):
    api, number, _key = ordinary_yandex(yandex_account, 1, ready=True)
    scan(yandex_account, user, BARCODE)
    saved = cargo.open_editor(yandex_account, number, user)
    token = saved['units'][0]['id']
    wanted = [{'id': 'box-1', 'units': [token]}, {'id': 'new-box', 'units': [token]}]
    original = api.order_boxes

    def timeout(*args):
        original(*args)
        raise yandex_client.YandexError('timeout')

    write = Mock(side_effect=timeout)
    monkeypatch.setattr(api, 'order_boxes', write)
    with pytest.raises(yandex_client.YandexError):
        cargo.save_editor(yandex_account, number, user, saved['revision'], wanted)
    assert cargo.get(yandex_account['id'], number)['status'] == 'uncertain'
    with pytest.raises(ValueError):
        cargo.labels_allowed(yandex_account['id'], number)
    confirmed = cargo.save_editor(yandex_account, number, user, saved['revision'], wanted)
    assert confirmed['status'] == 'confirmed' and len(confirmed['boxes']) == 2
    assert write.call_count == 1


def test_invalid_layout_does_not_delete_units_or_change_codes(yandex_account, user):
    _api, number, _key = ordinary_yandex(yandex_account)
    scan(yandex_account, user, BARCODE)
    saved = cargo.open_editor(yandex_account, number, user)
    before = cargo.get(yandex_account['id'], number)
    with pytest.raises(ValueError):
        cargo.save_editor(yandex_account, number, user, saved['revision'], [{'id': 'bad', 'units': [saved['units'][0]['id']]}])
    assert cargo.get(yandex_account['id'], number) == before


def test_download_and_archive_have_the_current_complete_set(yandex_account, user, client):
    from app.core import assembly_stickers, sticker_archive
    _api, number, _key = ordinary_yandex(yandex_account, 1, ready=True)
    scan(yandex_account, user, BARCODE)
    old = assembly_stickers.load(yandex_account, number, user)
    original = db.query_one('SELECT * FROM sticker_versions')
    saved = cargo.open_editor(yandex_account, number, user)
    token = saved['units'][0]['id']
    cargo.save_editor(yandex_account, number, user, saved['revision'],
                      [{'id': 'box-1', 'units': [token]}, {'id': 'new-box', 'units': [token]}])
    pdf = assembly_stickers.load(yandex_account, number, user)
    assert len(PdfReader(io.BytesIO(pdf)).pages) == 2
    assert len(db.query('SELECT * FROM sticker_versions')) == 1
    assert bytes(db.query_one('SELECT pdf FROM sticker_versions')['pdf']) == old
    assert sticker_archive.version(original['id'])['pdf'] == pdf
    assert client.get(f'/api/stickers/{original["id"]}.pdf').content == pdf


def test_ozon_ship_timeout_recovers_related_postings_without_reshipping(account, user, monkeypatch):
    from app.markets.ozon.client import OzonError
    api, number, _sku = ordinary_ozon(account)
    original = api.ship

    def timeout(*args):
        original(*args)
        raise OzonError('timeout')

    write = Mock(side_effect=timeout)
    monkeypatch.setattr(api, 'ship', write)
    assert scan(account, user, number)['status'] == 'error'
    assert cargo.split_unknown(account['id'], number)
    with pytest.raises(ValueError):
        cargo.labels_allowed(account['id'], number)
    recovered = scan(account, user, BARCODE)
    assert recovered['status'] == 'ok', recovered
    assert recovered['state']['total'] == 1
    assert len(ozon_cargo.children(account['id'], number)) == 2
    assert write.call_count == 1


def test_ozon_unconfirmed_split_timeout_blocks_retry_and_labels(account, user, monkeypatch):
    from app.markets.ozon.client import OzonError
    api, number, _sku = ordinary_ozon(account)
    write = Mock(side_effect=OzonError('timeout'))
    monkeypatch.setattr(api, 'ship', write)
    assert scan(account, user, number)['status'] == 'error'
    assert scan(account, user, number)['status'] == 'error'
    assert write.call_count == 1
    with pytest.raises(ValueError):
        cargo.labels_allowed(account['id'], number)


def test_marked_ozon_ship_timeout_recovers_all_exact_codes(account, user, monkeypatch):
    from app.markets.ozon.client import OzonError
    api, number, _sku = marked_ozon(account, quantity=2)
    original = api.ship

    def timeout(*args):
        original(*args)
        raise OzonError('timeout')

    write = Mock(side_effect=timeout)
    monkeypatch.setattr(api, 'ship', write)
    for value in (BARCODE, code(), BARCODE, code('ABCDEF1234568')):
        scan(account, user, value)
    finish_job(account, number)
    marking.process(account, number, force=True)
    rows = db.query('SELECT * FROM marking_orders WHERE status=\'accepted\'')
    assert len(rows) == 2
    assert {c['raw'] for r in rows for c in marking.codes(dict(r), full=True)} == {code(), code('ABCDEF1234568')}
    assert write.call_count == 1
    assert packing.state(user)['sticker_download']['ready']


def test_yandex_marked_units_keep_identity_when_remote_boxes_are_reordered(yandex_account, user):
    from app.markets.yandex import cargo as provider
    api, number, key = marked_yandex(yandex_account, quantity=2)
    for value in (BARCODE, code(), BARCODE, code('ABCDEF1234568')):
        scan(yandex_account, user, value)
    finish_job(yandex_account, number)
    state = packing.state(user)
    assert state['marking']['status'] == 'accepted', state
    raw = api.order_details(number)
    raw['delivery']['boxesLayout'].reverse()
    decoded = provider.decode(raw)
    for box, actual in zip(decoded, raw['delivery']['boxesLayout'], strict=True):
        value = actual['items'][0]['instances'][0]['cis']
        unit = 1 if value == code() else 2
        assert box['units'] == [cargo.token(key, unit)]
    # Layout state contains unit references, never the plaintext GS code.
    assert code() not in json.dumps(state['cargo'])
    assert len(db.query('SELECT * FROM marking_codes WHERE active=1')) == 2


def test_yandex_external_changes_are_not_overwritten(yandex_account, user, monkeypatch):
    api, number, _key = ordinary_yandex(yandex_account, 1, ready=True)
    scan(yandex_account, user, BARCODE)
    saved = cargo.open_editor(yandex_account, number, user)
    api.order_boxes(str(api._orders[number]['campaignId']), number,
                    [{'items': [{'id': api._orders[number]['items'][0]['id'], 'fullCount': 1}]}])
    write = Mock(side_effect=AssertionError('must not overwrite external layout'))
    monkeypatch.setattr(api, 'order_boxes', write)
    unit = saved['units'][0]['id']
    with pytest.raises(ValueError, match='изменена на Маркете'):
        cargo.save_editor(yandex_account, number, user, saved['revision'],
                          [{'id': 'box-1', 'units': [unit]}, {'id': 'new-box', 'units': [unit]}])
    assert not write.called
    assert cargo.get(yandex_account['id'], number)['revision'] == saved['revision']


def test_save_rejects_stale_revision_foreign_unit_and_wrong_actor(yandex_account, user):
    _api, number, _key = ordinary_yandex(yandex_account, 1, ready=True)
    scan(yandex_account, user, BARCODE)
    saved = cargo.open_editor(yandex_account, number, user)
    for revision, boxes, actor in (
        (saved['revision'] + 1, saved['boxes'], user),
        (False, saved['boxes'], user),
        (saved['revision'], [{'id': 'box-1', 'units': ['foreign-unit']}], user),
        (saved['revision'], saved['boxes'], {'id': -100}),
    ):
        with pytest.raises(ValueError):
            cargo.save_editor(yandex_account, number, actor, revision, boxes)
    assert cargo.get(yandex_account['id'], number) == saved


def test_cargo_endpoints_require_csrf_and_an_active_order(yandex_account, user, client):
    assert client.post('/api/pack/cargo/open', headers={'X-CSRF-Token': ''}).status_code == 403
    assert client.post('/api/pack/cargo/save', json={}, headers={'X-CSRF-Token': ''}).status_code == 403
    assert client.post('/api/pack/cargo/open').status_code == 409
    _api, number, _key = ordinary_yandex(yandex_account, 1, ready=True)
    scan(yandex_account, user, BARCODE)
    opened = client.post('/api/pack/cargo/open')
    assert opened.status_code == 200, opened.text
    saved = opened.json()['state']['cargo']
    result = client.post('/api/pack/cargo/save', json={'revision': saved['revision'] + 1, 'boxes': saved['boxes']})
    assert result.status_code == 409
    assert cargo.get(yandex_account['id'], number)['revision'] == saved['revision']


def test_force_complete_cannot_skip_boxes(yandex_account, user):
    _api, number, _key = ordinary_yandex(yandex_account)
    scan(yandex_account, user, BARCODE)
    scan(yandex_account, user, BARCODE)
    result = packing.complete(user)
    assert result['status'] == 'error'
    assert 'всех грузомест' in result['message']
    assert packing.state(user)['active']


def test_archive_zip_and_chz_retention_use_current_box_labels(yandex_account, user, client):
    from app.core import assembly_stickers, sticker_archive
    from zipfile import ZipFile
    _api, number, _key = ordinary_yandex(yandex_account, 1, ready=True)
    scan(yandex_account, user, BARCODE)
    assembly_stickers.load(yandex_account, number, user)
    original = db.query_one('SELECT * FROM sticker_versions')
    saved = cargo.open_editor(yandex_account, number, user)
    unit = saved['units'][0]['id']
    cargo.save_editor(yandex_account, number, user, saved['revision'],
                      [{'id': 'box-1', 'units': [unit]}, {'id': 'new-box', 'units': [unit]}])
    assembly_stickers.load(yandex_account, number, user)
    db.execute("UPDATE sticker_orders SET created_at='2020-01-01T00:00:00+00:00'")
    db.execute("UPDATE sticker_versions SET saved_at='2020-01-01T00:00:00+00:00'")
    sticker_archive.purge()
    rows, total = sticker_archive.search(since=db.now_iso()[:10], state='saved')
    assert total == 1 and rows[0]['version_id'] == original['id']
    assert marking.retained(original['archive_id'])
    result = client.post('/api/stickers/archive.zip', json=[original['id']])
    assert result.status_code == 200, result.text
    with ZipFile(io.BytesIO(result.content)) as archive:
        assert len(archive.namelist()) == 1
        assert len(PdfReader(io.BytesIO(archive.read(archive.namelist()[0]))).pages) == 2


def test_http200_with_wrong_layout_is_not_success_and_never_writes_twice(yandex_account, user, monkeypatch):
    api, number, _key = ordinary_yandex(yandex_account, 1, ready=True)
    scan(yandex_account, user, BARCODE)
    saved = cargo.open_editor(yandex_account, number, user)
    unit = saved['units'][0]['id']
    wanted = [{'id': 'box-1', 'units': [unit]}, {'id': 'new-box', 'units': [unit]}]
    write = Mock(return_value={'status': 'OK'})  # HTTP200 без фактического применения
    monkeypatch.setattr(api, 'order_boxes', write)
    for _ in range(2):
        with pytest.raises(ValueError, match='не подтвердил количество'):
            cargo.save_editor(yandex_account, number, user, saved['revision'], wanted)
        assert cargo.get(yandex_account['id'], number)['status'] == 'uncertain'
    assert write.call_count == 1
    assert packing.complete(user)['status'] == 'error'


def test_partial_and_whole_identical_marked_units_have_distinct_codes(yandex_account, user):
    from app.markets.yandex import cargo as provider
    raw = {'items': [{'id': 100, 'offerId': 'offer', 'count': 2,
                      'requiredInstanceTypes': ['CIS'], 'instances': [{'cis': code()}, {'cis': code('ABCDEF1234568')}]}]}
    units = cargo.units(provider.products(raw))
    wanted = [{'id': 'whole', 'units': [units[0]['id']]},
              {'id': 'part1', 'units': [units[1]['id']]}, {'id': 'part2', 'units': [units[1]['id']]}]
    checked = cargo.validate({'units': units, 'boxes': []}, wanted)
    marks = [{'item_key': '100', 'unit_no': 1, 'raw': code()},
             {'item_key': '100', 'unit_no': 2, 'raw': code('ABCDEF1234568')}]
    boxes = provider.payload(raw, units, checked, marks)
    raw['delivery'] = {'boxesLayout': [{**box, 'boxId': index, 'barcode': str(index)} for index, box in enumerate(boxes, 1)]}
    raw['delivery']['boxesLayout'].reverse()
    decoded = provider.decode(raw)
    assert [box['units'] for box in decoded] == [wanted[2]['units'], wanted[1]['units'], wanted[0]['units']]
    with pytest.raises(ValueError, match='отдельно'):
        cargo.validate({'units': units, 'boxes': []},
                       [{'id': 'bad', 'units': [u['id'] for u in units]}, {'id': 'part', 'units': [units[0]['id']]}])


def test_identical_unmarked_goods_in_four_places_can_be_reopened(yandex_account, user):
    _api, number, _key = ordinary_yandex(yandex_account, 2, ready=True)
    scan(yandex_account, user, BARCODE)
    saved = cargo.open_editor(yandex_account, number, user)
    first, second = [u['id'] for u in saved['units']]
    boxes = [{'id': 'first-a', 'units': [first]}, {'id': 'first-b', 'units': [first]},
             {'id': 'second-a', 'units': [second]}, {'id': 'second-b', 'units': [second]}]
    confirmed = cargo.save_editor(yandex_account, number, user, saved['revision'], boxes)
    assert cargo.open_editor(yandex_account, number, user)['boxes'] == confirmed['boxes']
    packing.release(user)
    reopened = scan(yandex_account, user, number)
    assert reopened['status'] == 'ok', reopened
    assert reopened['state']['total'] == 2
    assert len(reopened['state']['cargo']['boxes']) == 4


def test_parallel_cargo_request_is_a_conflict_not_a_server_error(account, user, client):
    db.execute('INSERT INTO order_operations VALUES(?,?,?,?)',
               (account['id'], f'@scan-user:{user["id"]}', 'another-request', '2999-01-01T00:00:00+00:00'))
    assert client.post('/api/pack/cargo/open').status_code == 409
    assert client.post('/api/pack/cargo/save', json={}).status_code == 409
