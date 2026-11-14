"""Compact AI 93 retains original bytes in the API, storage and printed duplicate."""
import io
from copy import deepcopy

import pytest
import zxingcpp
from pypdf import PdfReader

from app.core import db, marking, marking_codes, packing
from app.core.config import settings
from tests.core.test_marking import BARCODE, GTIN, code, finish_job, marked_ozon, marked_yandex, scan

SERIAL = 'AbC!>QwERt"9x'
CHECK = 'aB12'


def compact(serial=SERIAL, check=CHECK):
    return '01' + GTIN + '21' + serial + '93' + check


def separated(value):
    return value[:31] + '\x1d' + value[31:]


def test_known_compact_profile_preserves_bytes_and_separates_fields():
    raw = compact()
    parsed = marking_codes.parse(raw)
    assert len(raw) == 37 and parsed.raw == raw and parsed.compact
    assert parsed.gtin == GTIN and parsed.serial == SERIAL
    assert parsed.fields == (('01', GTIN), ('21', SERIAL), ('93', CHECK))
    assert marking_codes.short(raw) == raw[:31]
    assert marking_codes.equivalent(raw, separated(raw))
    assert not marking_codes.equivalent(raw, compact(check='bad!'))
    assert not marking_codes.equivalent(raw, raw[:31])
    assert marking_codes.parse(']d2' + raw + '\r\n').raw == raw


@pytest.mark.parametrize('raw', [compact(), separated(compact()), code()])
def test_pdf_and_image_decode_to_the_exact_original_payload(raw):
    parsed = marking_codes.parse(raw)
    decoded = zxingcpp.read_barcode(marking_codes.image(parsed))
    assert decoded.content_type == zxingcpp.ContentType.GS1
    assert decoded.bytes == raw.encode('ascii')
    pdf = PdfReader(io.BytesIO(marking_codes.pdf([parsed])))
    decoded_pdf = zxingcpp.read_barcode(list(pdf.pages[0].images)[0].image)
    assert decoded_pdf.bytes == raw.encode('ascii')
    assert decoded_pdf.content_type == zxingcpp.ContentType.GS1


@pytest.mark.parametrize('raw', [compact()[:-1], compact()+'x', compact().replace('93', '92'),
                               compact(serial=SERIAL+'z'), compact(serial=SERIAL[:-1]),
                               compact().replace('21', '22', 1), compact()[:31],
                               compact().replace(GTIN, GTIN[:-1]+'0'), compact()+'\x00',
                               compact()[:31]+'\\u001d'+compact()[31:]])
def test_unknown_truncated_or_literal_escaped_codes_are_not_repaired(raw):
    with pytest.raises(ValueError):
        marking_codes.parse(raw)


@pytest.mark.parametrize('market', ['ozon', 'yandex'])
@pytest.mark.parametrize('ready', [False, True])
def test_scan_retains_original_compact_code_and_requires_market_acceptance(account, yandex_account, user, market, ready):
    if market == 'ozon':
        shop = account
        api, number, key = marked_ozon(shop, status='awaiting_deliver' if ready else 'awaiting_packaging')
    else:
        shop = yandex_account
        api, number, key = marked_yandex(shop)
        if ready:
            # Market forbids new FBS identifiers in READY_TO_SHIP. Existing
            # exact saved identifiers can still be checked in this state.
            api._orders[number]['substatus'] = 'READY_TO_SHIP'
            api._orders[number]['items'][0]['instances'] = [{'cisFull': compact(), 'cis': compact()[:31]}]
    db.kv_set(marking.KV_AUTO, '1')
    scan(shop, user, BARCODE)
    saved_scan = scan(shop, user, compact())
    assert saved_scan['action'] == 'marking_saved', saved_scan
    assert saved_scan['marking_print']['kind'] == 'marking:duplicate'
    assert packing.complete(user)['status'] == 'error'
    saved = marking.order(shop['id'], number)
    mark = marking.codes(saved, full=True)[0]
    assert mark['raw'] == compact() and mark['serial'] == SERIAL
    pdf = PdfReader(io.BytesIO(marking.print_pdf([mark['id']])))
    assert zxingcpp.read_barcode(list(pdf.pages[0].images)[0].image).bytes == compact().encode()
    finish_job(shop, number)
    assert marking.order(shop['id'], number)['status'] == 'accepted'
    assert packing.state(user)['items'][0]['marking']['status'] == 'accepted'
    if market == 'ozon':
        assert api.marking_requests[-1]['products'][0]['exemplars'][0]['marks'][0]['mark'] == compact()
    elif not ready:
        assert api.box_requests[-1][0]['items'][0]['instances'][0]['cis'] == compact()


@pytest.mark.parametrize('reverse', [False, True])
def test_scanning_the_other_supported_representation_is_a_duplicate(account, user, reverse):
    api, number, key = marked_ozon(account)
    first, second = (separated(compact()), compact()) if reverse else (compact(), separated(compact()))
    db.kv_set(marking.KV_AUTO, '1')
    scan(account, user, BARCODE)
    scan(account, user, first)
    result = scan(account, user, second)
    assert result['status'] == 'warning', result
    assert 'marking_print' not in result
    rows = marking.codes(marking.order(account['id'], number), full=True)
    assert len(rows) == 1 and rows[0]['raw'] == first


@pytest.mark.parametrize('market', ['ozon', 'yandex'])
@pytest.mark.parametrize('wrong_crypto', [False, True])
def test_normalized_readback_requires_the_full_matching_crypto(account, yandex_account, user, monkeypatch, market, wrong_crypto):
    raw = compact()
    echo = separated(compact(check='bad!') if wrong_crypto else raw)
    shop = account if market == 'ozon' else yandex_account
    api, number, key = marked_ozon(shop) if market == 'ozon' else marked_yandex(shop)
    (_ozon_echo if market == 'ozon' else _yandex_echo)(api, echo, monkeypatch)
    scan(shop, user, BARCODE)
    scan(shop, user, raw)
    finish_job(shop, number)
    saved = marking.order(shop['id'], number)
    assert (saved['status'] == 'accepted') is (not wrong_crypto), saved
    assert marking.codes(saved, full=True)[0]['raw'] == raw


@pytest.mark.parametrize('market', ['ozon', 'yandex'])
def test_two_compact_units_keep_original_codes_when_split(account, yandex_account, user, monkeypatch, market):
    monkeypatch.setattr(settings, 'split_orders_on_pack', True)
    shop = account if market == 'ozon' else yandex_account
    api, number, key = marked_ozon(shop, quantity=2) if market == 'ozon' else marked_yandex(shop, quantity=2)
    values = [compact(), compact(serial='AbC!>QwERt"9y')]
    for value in values:
        scan(shop, user, BARCODE)
        assert scan(shop, user, value)['action'] == 'marking_saved'
    finish_job(shop, number)
    assert marking.order(shop['id'], number)['status'] == 'accepted'
    assert sorted(r['raw'] for s in db.query('SELECT account_id,order_id FROM marking_orders WHERE account_id=?', (shop['id'],))
                  for r in marking.codes(marking.order(s['account_id'], s['order_id']), full=True)) == sorted(values)


def _ozon_echo(api, echo, monkeypatch):
    status = api.exemplars_status
    get = api.posting_get
    def status_echo(*args):
        data = status(*args)
        for p in data.get('products') or []:
            for e in p['exemplars']:
                for m in e.get('marks') or []:
                    if m['mark_type'] == 'mandatory_mark':
                        m['mark'] = echo
        return data
    def posting_echo(*args, **kwargs):
        data = deepcopy(get(*args, **kwargs))
        for p in (data.get('product_exemplars') or {}).get('products') or []:
            for e in p['exemplars']:
                e['mandatory_mark'] = echo
        return data
    monkeypatch.setattr(api, 'exemplars_status', status_echo)
    monkeypatch.setattr(api, 'posting_get', posting_echo)


def _yandex_echo(api, echo, monkeypatch):
    get = api.order_details
    def details_echo(*args):
        data = deepcopy(get(*args))
        for item in data['items']:
            for instance in item.get('instances') or []:
                instance['cisFull'] = echo
        return data
    monkeypatch.setattr(api, 'order_details', details_echo)


@pytest.mark.parametrize('vendor', ['ozon', 'yandex'])
def test_wire_json_preserves_compact_quote_and_does_not_add_gs(vendor):
    import json
    import httpx
    from app.markets.ozon.client import OzonClient
    from app.markets.yandex.client import YandexClient
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200)
    api = OzonClient('test', 'test') if vendor == 'ozon' else YandexClient('9000001', 'test')
    api._client.close()
    api._client = httpx.Client(base_url='https://api-seller.ozon.ru' if vendor == 'ozon' else 'https://api.partner.market.yandex.ru',
                              transport=httpx.MockTransport(respond))
    try:
        if vendor == 'ozon':
            api.exemplars_set({'posting_number': 'test', 'products': [{'mark': compact()}]})
            got = json.loads(requests[0].content)['products'][0]['mark']
        else:
            api.order_boxes('123', '456', [{'items': [{'id': 1, 'fullCount': 1, 'instances': [{'cis': compact()}]}]}])
            got = json.loads(requests[0].content)['boxes'][0]['items'][0]['instances'][0]['cis']
        assert got == compact()
        assert b'\\u001d' not in requests[0].content and b'\x1d' not in requests[0].content
    finally:
        api.close()


def test_93_inside_the_serial_does_not_change_the_fixed_field_boundary():
    serial = 'Ab93>QwERt"9x'
    parsed = marking_codes.parse(compact(serial=serial))
    assert parsed.serial == serial and parsed.fields[-1] == ('93', CHECK)


@pytest.mark.parametrize('market', ['ozon', 'yandex'])
def test_split_confirmation_accepts_equivalent_full_codes_without_rewriting_them(account, yandex_account, user, monkeypatch, market):
    monkeypatch.setattr(settings, 'split_orders_on_pack', True)
    shop = account if market == 'ozon' else yandex_account
    api, number, key = marked_ozon(shop, quantity=2) if market == 'ozon' else marked_yandex(shop, quantity=2)
    method = 'exemplars_get' if market == 'ozon' else 'order_details'
    original = getattr(api, method)
    def readback(*args, **kwargs):
        return _separated_readback(deepcopy(original(*args, **kwargs)))
    monkeypatch.setattr(api, method, readback)
    expected = [compact(), compact(serial='AbC!>QwERt"9y')]
    for raw in expected:
        scan(shop, user, BARCODE)
        assert scan(shop, user, raw)['action'] == 'marking_saved'
    finish_job(shop, number)
    assert marking.order(shop['id'], number)['status'] == 'accepted'
    got = [c['raw'] for o in db.query('SELECT account_id,order_id FROM marking_orders WHERE account_id=?', (shop['id'],))
           for c in marking.codes(marking.order(o['account_id'], o['order_id']), full=True)]
    assert sorted(got) == sorted(expected)


def _separated_readback(value):
    if isinstance(value, dict):
        return {k: _separated_readback(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_separated_readback(v) for v in value]
    if isinstance(value, str) and marking_codes.COMPACT_93.fullmatch(value):
        return separated(value)
    return value


def test_two_parts_with_two_representations_still_refer_to_one_physical_unit():
    from app.markets.yandex import cargo
    raw = {'items': [{'id': 100, 'offerId': 'one', 'count': 1, 'instances': [{'cisFull': compact()}]}],
           'delivery': {'boxesLayout': [
               {'boxId': 1, 'barcode': 'one', 'items': [{'id': 100, 'partialCount': {'current': 1, 'total': 2},
                                                       'instances': [{'cisFull': compact()}]}]},
               {'boxId': 2, 'barcode': 'two', 'items': [{'id': 100, 'partialCount': {'current': 2, 'total': 2},
                                                       'instances': [{'cisFull': separated(compact())}]}]}]}}
    boxes = cargo.decode(raw)
    assert len(boxes) == 2 and boxes[0]['units'] == boxes[1]['units'] and len(boxes[0]['units']) == 1
