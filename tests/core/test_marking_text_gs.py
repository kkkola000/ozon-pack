"""Scanner <gs> notation becomes real GS without changing the marking data."""
import base64
import io
import json

import httpx
import pytest
import zxingcpp
from pypdf import PdfReader

from app.core import accounts, db, marking, marking_codes, packing
from app.markets.ozon.client import OzonClient
from app.markets.yandex.client import YandexClient
from tests.core.test_compact_marking import _ozon_echo, _yandex_echo
from tests.core.test_marking import BARCODE, GTIN, code, finish_job, marked_ozon, marked_yandex, scan

SERIAL = '5a2,bCd)EfGH&'
RAW = code(SERIAL, base64.b64encode(bytes(range(224, 256))).decode('ascii'))


def text_scan(raw=RAW, token='<gs>', leading=True):
    return (token if leading else '') + raw.replace(marking_codes.GS, token)


def joined_scan(raw=RAW):
    return raw.replace(marking_codes.GS, '')


@pytest.mark.parametrize('token', ['<gs>', '<GS>', r'\<gs>'])
@pytest.mark.parametrize('leading', [False, True])
@pytest.mark.parametrize('prefix', ['', ']d2', ']d8'])
def test_scanner_notation_preserves_all_fields(token, leading, prefix):
    scanned = prefix + text_scan(token=token, leading=leading) + '\r\n'
    assert marking_codes.looks_like(scanned)
    parsed = marking_codes.parse(scanned)
    assert parsed.raw == RAW
    assert parsed.gtin == GTIN and parsed.serial == SERIAL
    assert parsed.fields == marking_codes.parse(RAW).fields
    assert marking_codes.short(scanned) == '01' + GTIN + '21' + SERIAL
    assert marking_codes.equivalent(scanned, RAW)
    assert marking_codes.comparison_key(scanned) == marking_codes.comparison_key(RAW)


@pytest.mark.parametrize('serial', ['a<gs>bCdE1234', 'a<GS>bCdE1234', r'a\<gs>bCd1234'])
def test_literal_text_inside_an_already_valid_serial_is_not_rewritten(serial):
    native = code(serial)
    parsed = marking_codes.parse(']d2' + native)
    assert parsed.raw == native and parsed.serial == serial


@pytest.mark.parametrize('serial', [SERIAL, 'A91,bCd)EfGH&', 'a<gs>bCdE1234'])
def test_joined_91_92_restores_only_fixed_separators(serial):
    native = RAW.replace(SERIAL, serial)
    scanned = joined_scan(native)
    assert len(scanned) == 83 and marking_codes.looks_like(scanned)
    parsed = marking_codes.parse(scanned)
    assert parsed.raw == native and parsed.serial == serial and not parsed.compact
    assert marking_codes.equivalent(scanned, native)
    assert parsed.fields == marking_codes.parse(native).fields


@pytest.mark.parametrize('scanned', [
    joined_scan()[:-1], joined_scan() + 'a', joined_scan().replace('92', '90', 1),
    joined_scan().replace('91ABCD', '91ABC'), joined_scan().replace('91ABCD', '91ABCDE'),
    joined_scan().replace(SERIAL, SERIAL + 'a'), joined_scan()[:-2] + '%=',
])
def test_ambiguous_or_damaged_joined_profiles_are_not_guessed(scanned):
    with pytest.raises(ValueError):
        marking_codes.parse(scanned)


@pytest.mark.parametrize('scanned', [
    text_scan().rsplit('<gs>92', 1)[0],
    text_scan().replace(GTIN, GTIN[:-1] + '0'),
    text_scan() + '<gs>91EFGH',
    text_scan() + '\x00',
    text_scan(token=r'\u001d'),
    text_scan(token='<RS>'),
    text_scan(token='[GS]'),
])
def test_text_notation_does_not_bypass_validation_or_decode_json_escapes(scanned):
    with pytest.raises(ValueError):
        marking_codes.parse(scanned)


def assert_pdf_payload(pdf):
    document = PdfReader(io.BytesIO(pdf))
    decoded = zxingcpp.read_barcode(list(document.pages[0].images)[0].image)
    assert decoded.content_type == zxingcpp.ContentType.GS1
    assert decoded.bytes == RAW.encode('ascii')


@pytest.mark.parametrize('scanned', [text_scan(), joined_scan()])
def test_duplicate_contains_real_separators_and_exact_serial_and_crypto(scanned):
    parsed = marking_codes.parse(scanned)
    decoded = zxingcpp.read_barcode(marking_codes.image(parsed))
    assert decoded.bytes == RAW.encode('ascii')
    assert_pdf_payload(marking_codes.pdf([parsed]))


@pytest.mark.parametrize('market,status', [
    ('ozon', 'awaiting_packaging'), ('ozon', 'awaiting_deliver'), ('yandex', 'STARTED'),
])
@pytest.mark.parametrize('scanned', [text_scan(token=r'\<gs>'), joined_scan()])
def test_scanner_notation_is_saved_sent_and_confirmed(account, yandex_account, user, market, status, scanned):
    shop = account if market == 'ozon' else yandex_account
    api, number, _ = marked_ozon(shop, status=status) if market == 'ozon' else marked_yandex(shop)
    db.kv_set(marking.KV_AUTO, '1')
    scan(shop, user, BARCODE)
    result = scan(shop, user, scanned)
    assert result['action'] == 'marking_saved', result
    assert result['marking_print']['kind'] == 'marking:duplicate'
    assert not result['state']['complete']
    assert packing.complete(user)['status'] == 'error'
    saved = marking.order(shop['id'], number)
    marks = marking.codes(saved, full=True)
    assert len(marks) == 1 and marks[0]['raw'] == RAW
    assert_pdf_payload(marking.print_pdf([marks[0]['id']]))
    duplicate = scan(shop, user, RAW)
    assert duplicate['status'] == 'warning' and 'marking_print' not in duplicate
    finish_job(shop, number)
    assert marking.order(shop['id'], number)['status'] == 'accepted'
    assert packing.state(user)['items'][0]['marking']['status'] == 'accepted'
    if market == 'ozon':
        assert api.marking_requests[-1]['products'][0]['exemplars'][0]['marks'][0]['mark'] == RAW
    else:
        assert api.box_requests[-1][0]['items'][0]['instances'][0]['cis'] == RAW
    assert marking.archive_details(saved['archive_id'])['items'][0]['serial'] == SERIAL
    assert RAW not in json.dumps(result) and RAW not in str(db.query_one('SELECT code FROM marking_codes')['code'])


def test_text_notation_does_not_allow_reusing_a_code_in_another_order(account, user):
    _, number, _ = marked_ozon(account)
    scan(account, user, BARCODE)
    assert scan(account, user, text_scan())['action'] == 'marking_saved'
    finish_job(account, number)
    packing.release(user)
    second = accounts.get(accounts.create('ozon', 'Second shop', 'test-client-2', 'test-key-2'))
    marked_ozon(second)
    scan(second, user, BARCODE)
    rejected = scan(second, user, RAW)
    assert rejected['status'] == 'error' and rejected['message'].startswith('Данный Честный знак уже используется в заказе: ')
    assert db.count('SELECT COUNT(*) FROM marking_codes') == 1


@pytest.mark.parametrize('market', ['ozon', 'yandex'])
@pytest.mark.parametrize('wrong_crypto', [False, True])
def test_joined_readback_confirms_only_the_same_complete_code(account, yandex_account, user, monkeypatch, market, wrong_crypto):
    shop = account if market == 'ozon' else yandex_account
    api, number, _ = marked_ozon(shop) if market == 'ozon' else marked_yandex(shop)
    echo = RAW
    if wrong_crypto:
        signature = dict(marking_codes.parse(RAW).fields)['92']
        echo = RAW.replace(signature, ('A' if signature[0] != 'A' else 'B') + signature[1:])
    (_ozon_echo if market == 'ozon' else _yandex_echo)(api, joined_scan(echo), monkeypatch)
    scan(shop, user, BARCODE)
    assert scan(shop, user, joined_scan())['action'] == 'marking_saved'
    finish_job(shop, number)
    saved = marking.order(shop['id'], number)
    assert (saved['status'] == 'accepted') is (not wrong_crypto)
    assert marking.codes(saved, full=True)[0]['raw'] == RAW


@pytest.mark.parametrize('market', ['ozon', 'yandex'])
@pytest.mark.parametrize('scanned', [text_scan(), joined_scan()])
def test_wire_json_uses_single_gs_escapes_instead_of_scanner_notation(market, scanned):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200)
    api = OzonClient('test', 'test') if market == 'ozon' else YandexClient('9000001', 'test')
    api._client.close()
    api._client = httpx.Client(base_url='https://api-seller.ozon.ru' if market == 'ozon' else 'https://api.partner.market.yandex.ru',
                              transport=httpx.MockTransport(respond))
    normalized = marking_codes.parse(scanned).raw
    try:
        if market == 'ozon':
            api.exemplars_set({'posting_number': 'test', 'products': [{'exemplars': [
                {'marks': [{'mark': normalized, 'mark_type': 'mandatory_mark'}]}]}]})
            got = json.loads(requests[0].content)['products'][0]['exemplars'][0]['marks'][0]['mark']
        else:
            api.order_boxes('123', '456', [{'items': [{'id': 1, 'fullCount': 1, 'instances': [{'cis': normalized}]}]}])
            got = json.loads(requests[0].content)['boxes'][0]['items'][0]['instances'][0]['cis']
        assert got == RAW
        wire = requests[0].content
        assert wire.count(b'\\u001d') == 2
        assert b'\\\\u001d' not in wire and b'<gs>' not in wire and b'\x1d' not in wire
    finally:
        api.close()
