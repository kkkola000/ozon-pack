"""Required GTD/other data must survive the entire CZ and ready workflow."""

import pytest
from fastapi.testclient import TestClient

from app.core import db, exemplar_data, marking, packing
from app.core.config import settings
from app.main import app
from tests.core.test_marking import BARCODE, code, finish_job, marked_ozon, marked_yandex, scan

GTD = '10719040/131126/1234567'


def require_ozon(account, *, fields=('gtd',), quantity=1, status='awaiting_packaging', cz=True):
    api, number, sku = marked_ozon(account, quantity, status)
    raw = api._postings[number]
    if not cz:
        raw['requirements']['products_requiring_mandatory_mark'] = []
        db.execute('UPDATE posting_items SET mandatory_mark=0 WHERE account_id=?', (account['id'],))
    for f in fields:
        raw['requirements']['products_requiring_' + {'uin': 'jw_uin'}.get(f, f)] = [int(sku)]
    return api, number, sku


def enter(account, user, number, key, fields, unit=1):
    saved = marking.order(account['id'], number)
    exemplar_data.save(account, user, number, {'revision': saved['revision'], 'items': [
        {'key': key, 'unit': unit, 'fields': fields}]})


@pytest.mark.parametrize('status', ['awaiting_packaging', 'awaiting_deliver'])
@pytest.mark.parametrize('cz', [True, False])
def test_ozon_gtd_is_separate_required_input_and_verified_before_completion(account, user, status, cz):
    api, number, sku = require_ozon(account, status=status, cz=cz)
    opened = scan(account, user, BARCODE)
    field = opened['state']['items'][0]['exemplar_data']['units'][0]['fields'][0]
    assert field['label'] == 'Указать ГТД' and field['value'] == ''
    assert not opened['state']['complete']
    assert packing.complete(user)['status'] == 'error'
    assert scan(account, user, number)['status'] == 'error'
    if cz:
        assert scan(account, user, code())['action'] == 'marking_saved'
        assert marking.order(account['id'], number)['status'] == 'collecting'
    assert not getattr(api, 'marking_requests', [])
    enter(account, user, number, sku, {'gtd': GTD})
    assert marking.order(account['id'], number)['status'] == 'queued'
    finish_job(account, number)
    saved = marking.order(account['id'], number)
    assert saved['status'] == 'accepted', saved
    e = api.marking_requests[-1]['products'][0]['exemplars'][0]
    assert e['gtd'] == GTD and e['is_gtd_absent'] is False
    assert api._postings[number]['product_exemplars']['products'][0]['exemplars'][0]['gtd'] == GTD
    if cz:
        assert e['marks'][0] == {'mark': code(), 'mark_type': 'mandatory_mark'}
        assert packing.state(user)['items'][0]['marking']['status'] == 'accepted'
    else:
        assert not packing.state(user)['items'][0].get('marking')
    assert scan(account, user, number)['action'] == 'completed'
    assert GTD not in saved['details']


def test_all_required_ozon_fields_are_transmitted_and_read_back(account, user):
    fields = {'gtd': GTD, 'rnpt': '12345678901234', 'imei': '490154203237518', 'weight': 1500}
    api, number, sku = require_ozon(account, fields=fields, cz=False)
    scan(account, user, BARCODE)
    enter(account, user, number, sku, fields)
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'
    e = api.marking_requests[-1]['products'][0]['exemplars'][0]
    assert {k: e[k] for k in ('gtd', 'rnpt', 'weight')} == {k: fields[k] for k in ('gtd', 'rnpt', 'weight')}
    assert {m['mark_type'] for m in e['marks']} == {'imei'}


def test_two_units_wait_for_each_gtd_and_do_not_cross_assign(account, user):
    api, number, sku = require_ozon(account, quantity=2, cz=False)
    scan(account, user, BARCODE)
    enter(account, user, number, sku, {'gtd': GTD}, 1)
    assert marking.order(account['id'], number)['status'] == 'collecting'
    enter(account, user, number, sku, {'gtd': GTD + '8'}, 2)
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'
    assert [e['gtd'] for e in api.marking_requests[0]['products'][0]['exemplars']] == [GTD, GTD + '8']


def test_gtd_rejection_is_field_error_and_can_be_corrected_without_rescanning_cz(account, user, monkeypatch):
    api, number, sku = require_ozon(account)
    scan(account, user, BARCODE)
    scan(account, user, code())
    enter(account, user, number, sku, {'gtd': GTD})
    marking.process(account, number, force=True)
    original = api.exemplars_status
    def rejected(*args):
        data = original(*args)
        data['products'][0]['exemplars'][0].update(gtd_check_status='ERROR', gtd_error_codes=['GTD_INVALID'])
        return data
    with monkeypatch.context() as patch:
        patch.setattr(api, 'exemplars_status', rejected)
        marking.process(account, number, force=True)
    saved = marking.order(account['id'], number)
    assert saved['status'] == 'error'
    assert marking.codes(saved)[0]['status'] == 'saved'
    assert packing.state(user)['items'][0]['exemplar_data']['units'][0]['fields'][0]['error']
    enter(account, user, number, sku, {'gtd': GTD + '8'})
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'
    assert len(marking.codes(saved)) == 1


def test_ozon_200_and_passed_wrong_gtd_never_unblock(account, user, monkeypatch):
    api, number, sku = require_ozon(account, cz=False)
    scan(account, user, BARCODE)
    enter(account, user, number, sku, {'gtd': GTD})
    marking.process(account, number, force=True)
    original = api.exemplars_status
    def wrong(*args):
        data = original(*args)
        data['products'][0]['exemplars'][0]['gtd'] = 'old'
        return data
    monkeypatch.setattr(api, 'exemplars_status', wrong)
    marking.process(account, number, force=True)
    assert marking.order(account['id'], number)['status'] == 'checking'
    assert packing.complete(user)['status'] == 'error'
    assert api._postings[number]['status'] == 'awaiting_packaging'


def test_readback_after_ozon_update_must_prove_gtd_applied(account, user, monkeypatch):
    api, number, sku = require_ozon(account, cz=False, status='awaiting_deliver')
    scan(account, user, BARCODE)
    enter(account, user, number, sku, {'gtd': GTD})
    monkeypatch.setattr(api, 'exemplars_update', lambda *_: None)
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] != 'accepted'
    assert packing.complete(user)['status'] == 'error'


@pytest.mark.parametrize('types', [['GTD'], ['GTD', 'RNPT', 'UIN'], ['CIS_OPTIONAL', 'GTD']])
def test_yandex_required_data_and_optional_cis_are_mandatory(yandex_account, user, types):
    api, number, key = marked_yandex(yandex_account)
    api._orders[number]['items'][0]['requiredInstanceTypes'] = types
    opened = scan(yandex_account, user, BARCODE)
    assert opened['state']['items'][0]['exemplar_data']
    assert not opened['state']['complete']
    if 'CIS_OPTIONAL' in types:
        assert opened['state']['items'][0]['marking']['status'] == 'waiting'
        scan(yandex_account, user, code())
    fields = {'gtd': GTD}
    if 'RNPT' in types:
        fields.update(rnpt='12345678901234')
    enter(yandex_account, user, number, key, fields)
    finish_job(yandex_account, number)
    saved = marking.order(yandex_account['id'], number)
    assert saved['status'] == 'accepted', saved
    assert api._orders[number]['substatus'] == 'READY_TO_SHIP'
    instance = api._orders[number]['items'][0]['instances'][0]
    assert all(instance[k] == v for k, v in fields.items())
    if 'CIS_OPTIONAL' in types:
        assert instance['cisFull'] == code()
    assert scan(yandex_account, user, number)['action'] == 'completed'


def test_ready_yandex_cannot_falsely_accept_new_gtd(yandex_account, user):
    api, number, key = marked_yandex(yandex_account)
    raw = api._orders[number]
    raw['substatus'] = 'READY_TO_SHIP'
    raw['items'][0]['requiredInstanceTypes'] = ['GTD']
    scan(yandex_account, user, BARCODE)
    enter(yandex_account, user, number, key, {'gtd': GTD})
    finish_job(yandex_account, number)
    assert marking.order(yandex_account['id'], number)['status'] == 'error'
    assert packing.complete(user)['status'] == 'error'
    assert not getattr(api, 'box_requests', [])


@pytest.mark.parametrize('market', ['ozon', 'yandex'])
def test_extra_data_survives_automatic_splitting(account, yandex_account, user, monkeypatch, market):
    monkeypatch.setattr(settings, 'split_orders_on_pack', True)
    if market == 'ozon':
        shop = account
        api, number, key = require_ozon(shop, quantity=2, cz=False)
    else:
        shop = yandex_account
        api, number, key = marked_yandex(shop, quantity=2)
        api._orders[number]['items'][0]['requiredInstanceTypes'] = ['GTD']
    scan(shop, user, BARCODE)
    enter(shop, user, number, key, {'gtd': GTD}, 1)
    enter(shop, user, number, key, {'gtd': GTD + '8'}, 2)
    finish_job(shop, number)
    assert marking.order(shop['id'], number)['status'] == 'accepted'
    if market == 'ozon':
        rows = db.query('SELECT child_id,marks_verified FROM cargo_split_children WHERE account_id=?', (shop['id'],))
        assert len(rows) == 2 and all(r['marks_verified'] for r in rows)
        snapshots = [marking.order(shop['id'], r['child_id']) for r in rows]
        assert sorted(exemplar_data.values(s)[key]['1']['gtd'] for s in snapshots) == [GTD, GTD + '8']
    else:
        assert len(api._orders[number]['delivery']['boxesLayout']) == 2
        assert sorted(b['items'][0]['instances'][0]['gtd'] for b in api._orders[number]['delivery']['boxesLayout']) == [GTD, GTD + '8']


def test_fields_reject_stale_revision_wrong_unit_empty_and_inflight(account, user):
    api, number, key = require_ozon(account, cz=False)
    scan(account, user, BARCODE)
    saved = marking.order(account['id'], number)
    payload = {'revision': saved['revision'], 'items': [{'key': key, 'unit': 1, 'fields': {'gtd': ''}}]}
    with pytest.raises(ValueError):
        exemplar_data.save(account, user, number, payload)
    payload['items'][0]['unit'] = 2
    with pytest.raises(ValueError):
        exemplar_data.save(account, user, number, payload)
    enter(account, user, number, key, {'gtd': GTD})
    with pytest.raises(ValueError):
        exemplar_data.save(account, user, number, payload)
    assert marking.order(account['id'], number)['status'] == 'queued'
    assert len(getattr(api, 'marking_requests', [])) == 0


def test_route_requires_session_csrf_and_current_owned_order(account, user):
    import re
    api, number, key = require_ozon(account, cz=False)
    scan(account, user, BARCODE)
    payload = {'revision': marking.order(account['id'], number)['revision'], 'items': [
        {'key': key, 'unit': 1, 'fields': {'gtd': GTD}}]}
    with TestClient(app, follow_redirects=False) as c:
        assert c.post('/api/pack/exemplar-data', json=payload).status_code == 401
        c.post('/login', data={'login': 'admin', 'password': 'test-admin-pass'})
        assert c.post('/api/pack/exemplar-data', json=payload).status_code == 403
        c.headers['X-CSRF-Token'] = re.search(r'name="csrf-token" content="([^"]*)"', c.get('/pack').text).group(1)
        assert c.post('/api/pack/exemplar-data', json=payload).status_code == 200
        assert c.post('/api/pack/exemplar-data', json=payload).status_code == 409


@pytest.mark.parametrize('market', ['ozon', 'yandex'])
def test_gtd_then_cz_keeps_both_full_values_during_split(account, yandex_account, user, monkeypatch, market):
    monkeypatch.setattr(settings, 'split_orders_on_pack', True)
    if market == 'ozon':
        shop = account
        api, number, key = require_ozon(shop, quantity=2)
    else:
        shop = yandex_account
        api, number, key = marked_yandex(shop, quantity=2)
        api._orders[number]['items'][0]['requiredInstanceTypes'] = ['CIS', 'GTD']
    scan(shop, user, BARCODE)
    enter(shop, user, number, key, {'gtd': GTD}, 1)
    enter(shop, user, number, key, {'gtd': GTD + '8'}, 2)
    assert marking.order(shop['id'], number)['status'] == 'collecting'
    scan(shop, user, code())
    scan(shop, user, BARCODE)
    scan(shop, user, code('ABCDEF1234568'))
    finish_job(shop, number)
    saved = marking.order(shop['id'], number)
    assert saved['status'] == 'accepted', saved
    if market == 'ozon':
        rows = db.query('SELECT child_id,marks_verified FROM cargo_split_children WHERE account_id=?', (shop['id'],))
        assert len(rows) == 2 and all(r['marks_verified'] for r in rows)
        for row in rows:
            child = marking.order(shop['id'], row['child_id'])
            c = marking.codes(child, full=True)[0]
            expected = GTD if c['raw'] == code() else GTD + '8'
            assert exemplar_data.values(child)[key]['1']['gtd'] == expected
    else:
        actual = api._orders[number]['items'][0]['instances']
        assert {(i['gtd'], i['cisFull']) for i in actual} == {(GTD, code()), (GTD + '8', code('ABCDEF1234568'))}


def test_accepted_cz_stays_green_when_gtd_requirement_is_added(account, user):
    api, number, key = marked_ozon(account)
    scan(account, user, BARCODE)
    scan(account, user, code())
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'
    api._postings[number]['requirements']['products_requiring_gtd'] = [int(key)]
    assert packing.complete(user)['status'] == 'error'
    state = packing.state(user)
    assert state['items'][0]['marking']['status'] == 'accepted'
    assert state['items'][0]['exemplar_data']['units'][0]['fields'][0]['name'] == 'gtd'
    enter(account, user, number, key, {'gtd': GTD})
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'
    assert marking.codes(marking.order(account['id'], number), full=True)[0]['raw'] == code()


def test_one_item_two_yandex_places_keeps_one_cz_and_same_gtd(yandex_account, user):
    from app.core import cargo
    api, number, key = marked_yandex(yandex_account)
    api._orders[number]['items'][0]['requiredInstanceTypes'] = ['CIS', 'GTD']
    scan(yandex_account, user, BARCODE)
    plan = cargo.open_editor(yandex_account, number, user)
    unit = plan['units'][0]['id']
    cargo.save_editor(yandex_account, number, user, plan['revision'],
                      [{'id': 'box-1', 'units': [unit]}, {'id': 'new-box', 'units': [unit]}])
    enter(yandex_account, user, number, key, {'gtd': GTD})
    scan(yandex_account, user, code())
    finish_job(yandex_account, number)
    assert marking.order(yandex_account['id'], number)['status'] == 'accepted'
    assert len(marking.codes(marking.order(yandex_account['id'], number))) == 1
    assert [b['items'][0]['instances'][0]['gtd'] for b in api.box_requests[-1]] == [GTD, GTD]


def test_stale_success_task_does_not_hide_remote_gtd_change(account, user, monkeypatch):
    api, number, key = require_ozon(account, cz=False)
    scan(account, user, BARCODE)
    enter(account, user, number, key, {'gtd': GTD})
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'
    api._postings[number]['product_exemplars']['products'][0]['exemplars'][0]['gtd'] = 'changed outside panel'
    monkeypatch.setattr(api, 'exemplars_update', lambda *_: None)
    assert packing.complete(user)['status'] == 'error'
    assert db.query_one('SELECT local_state FROM postings WHERE account_id=? AND posting_number=?',
                        (account['id'], number))['local_state'] != 'packed'


def test_upgrade_adds_columns_without_losing_saved_full_cz(account, user):
    api, number, key = marked_ozon(account)
    scan(account, user, BARCODE)
    scan(account, user, code())
    for column in ('details', 'field_errors'):
        db.execute('ALTER TABLE marking_orders DROP COLUMN ' + column)
    for column in ('marking_required', 'marking_checked_at'):
        db.execute('ALTER TABLE products DROP COLUMN ' + column)
    db.init_db()
    saved = marking.order(account['id'], number)
    assert saved['details'] == '' and saved['field_errors'] == '{}'
    assert marking.codes(saved, full=True)[0]['raw'] == code()
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'
