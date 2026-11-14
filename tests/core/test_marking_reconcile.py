"""Confirmed external removal invalidates active bindings without replaying them."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from app.core import db, exemplar_data, marking, packing, sticker_archive
from app.markets.ozon.client import OzonError
from tests.core.test_compact_marking import compact, separated
from tests.core.test_exemplar_data import GTD, enter
from tests.core.test_marking import BARCODE, finish_job, marked_ozon, scan


def accepted(account, user, quantity=1):
    api, number, sku = marked_ozon(account, quantity=quantity)
    for n in range(quantity):
        scan(account, user, BARCODE)
        scan(account, user, compact(serial='AbC!>QwERt"9' + str(n)))
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'
    return api, number, sku


def remove_remotely(api, number, unit=1):
    for product in api._marking_payloads[number]['products']:
        e = product['exemplars'][unit - 1]
        e['marks'] = [m for m in e.get('marks') or [] if m['mark_type'] != 'mandatory_mark']
    api._apply_exemplars(number)


def test_cancel_reconciles_removed_code_and_reopen_requires_rescan(account, user):
    api, number, sku = accepted(account, user)
    saved = marking.order(account['id'], number)
    row = marking.codes(saved)[0]
    pdf, _ = api.package_label_batch([number])
    assert pdf
    sticker_archive.save(saved['archive_id'], pdf)
    remove_remotely(api, number)
    released = packing.release(user)
    assert released['action'] == 'released' and not released['state']['active']
    current = marking.order(account['id'], number)
    assert current['status'] == 'collecting' and current['revision'] > saved['revision']
    assert marking.codes(current) == []
    removed = db.query_one('SELECT * FROM marking_codes WHERE id=?', (row['id'],))
    assert removed['active'] == 0 and removed['status'] == 'removed'
    assert marking.archive_details(saved['archive_id'])['items'] == []
    assert db.query_one('SELECT id FROM sticker_orders WHERE id=?', (saved['archive_id'],))
    assert bytes(db.query_one('SELECT pdf FROM sticker_versions WHERE archive_id=?', (saved['archive_id'],))['pdf']) == pdf
    with pytest.raises(LookupError):
        marking.print_pdf([row['id']])
    opened = scan(account, user, BARCODE)
    assert opened['state']['items'][0]['marking']['status'] == 'waiting'
    assert not opened['state']['complete']
    assert packing.complete(user)['status'] == 'error'
    assert scan(account, user, number)['status'] == 'error'
    marking.run_once()
    assert len(api.marking_requests) == 1


def test_explicit_rescan_of_the_same_removed_code_is_allowed_and_prints_again(account, user):
    api, number, sku = accepted(account, user)
    original = marking.codes(marking.order(account['id'], number), full=True)[0]
    db.kv_set(marking.KV_AUTO, '1')
    remove_remotely(api, number)
    packing.release(user)
    scan(account, user, BARCODE)
    rescanned = scan(account, user, separated(original['raw']))
    assert rescanned['status'] == 'ok' and 'marking_print' in rescanned, rescanned
    current = marking.order(account['id'], number)
    marks = marking.codes(current, full=True)
    assert len(marks) == 1 and marks[0]['id'] == original['id']
    assert marks[0]['raw'] == separated(original['raw'])
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'
    assert len(api.marking_requests) == 2


def test_deleting_one_code_keeps_the_other_unit_bound(account, user):
    api, number, sku = accepted(account, user, quantity=2)
    saved = marking.order(account['id'], number)
    first, second = marking.codes(saved, full=True)
    remove_remotely(api, number, 1)
    packing.release(user)
    current = marking.order(account['id'], number)
    active = marking.codes(current, full=True)
    assert len(active) == 1 and active[0]['id'] == second['id'] and active[0]['status'] == 'accepted'
    assert marking.archive_details(saved['archive_id'])['items'][0]['unit_no'] == 2
    scan(account, user, BARCODE)
    assert scan(account, user, first['raw'])['action'] == 'marking_saved'
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'
    assert len(marking.codes(current)) == 2


@pytest.mark.parametrize('phase', ['queued', 'checking', 'uncertain'])
def test_unconfirmed_codes_are_not_discarded_when_absent_remotely(account, user, phase):
    api, number, sku = marked_ozon(account)
    scan(account, user, BARCODE)
    scan(account, user, compact())
    db.execute('UPDATE marking_orders SET status=? WHERE account_id=? AND order_id=?', (phase, account['id'], number))
    packing.release(user)
    current = marking.order(account['id'], number)
    assert current['status'] == phase
    assert len(marking.codes(current)) == 1 and marking.codes(current)[0]['status'] == 'saved'
    assert not api.marking_requests


@pytest.mark.parametrize('failure', ['403', 'empty', 'partial', 'changed_ids'])
def test_failed_or_incomplete_refresh_does_not_erase_codes_or_prevent_cancel(account, user, monkeypatch, failure):
    api, number, sku = accepted(account, user, quantity=2 if failure == 'changed_ids' else 1)
    before = marking.order(account['id'], number)
    original = marking.codes(before, full=True)[0]['raw']
    if failure == '403':
        response = Mock(side_effect=OzonError('private payload', status=403))
    else:
        data = deepcopy(api.exemplars_get(number))
        if failure == 'empty':
            data = {}
        elif failure == 'partial':
            data['products'][0]['exemplars'] = []
        else:
            data['products'][0]['exemplars'][0]['exemplar_id'] += 100
            data['products'][0]['exemplars'][0]['marks'] = []
        response = Mock(return_value=data)
    monkeypatch.setattr(api, 'exemplars_get', response)
    canceled = packing.release(user)
    assert canceled['action'] == 'released' and canceled['status'] == 'warning'
    assert not canceled['state']['active']
    current = marking.order(account['id'], number)
    assert marking.codes(current, full=True)[0]['raw'] == original
    assert current['status'] == before['status']
    assert 'private payload' not in str(canceled)


def test_cancel_with_unchanged_remote_code_keeps_the_accepted_binding(account, user):
    api, number, sku = accepted(account, user)
    saved = marking.order(account['id'], number)
    original = marking.codes(saved, full=True)[0]
    assert packing.release(user)['status'] == 'ok'
    current = marking.order(account['id'], number)
    assert current['status'] == 'accepted'
    assert marking.codes(current, full=True)[0]['raw'] == original['raw']
    assert len(api.marking_requests) == 1


def test_next_scan_reconciles_deletion_without_cancellation(account, user):
    api, number, sku = accepted(account, user)
    remove_remotely(api, number)
    result = scan(account, user, BARCODE)
    assert result['status'] == 'error' and 'Честный знак' in result['message']
    assert result['state']['items'][0]['marking']['status'] == 'waiting'
    assert marking.codes(marking.order(account['id'], number)) == []


def test_pending_gtd_write_does_not_restore_externally_removed_accepted_code(account, user):
    api, number, sku = accepted(account, user)
    api._postings[number]['requirements']['products_requiring_gtd'] = [int(sku)]
    assert packing.complete(user)['status'] == 'error'
    enter(account, user, number, sku, {'gtd': GTD})
    assert marking.order(account['id'], number)['status'] == 'queued'
    remove_remotely(api, number)
    marking.process(account, number, force=True)
    current = marking.order(account['id'], number)
    assert current['status'] == 'collecting'
    assert marking.codes(current) == []
    assert exemplar_data.values(current)[sku]['1']['gtd'] == GTD
    assert len(api.marking_requests) == 1
    assert not api._marking_payloads[number]['products'][0]['exemplars'][0]['marks']


def test_requirements_added_before_dispatch_collect_inputs_instead_of_stalling(account, user):
    api, number, sku = marked_ozon(account)
    scan(account, user, BARCODE)
    scan(account, user, compact())
    api._postings[number]['requirements']['products_requiring_gtd'] = [int(sku)]
    marking.process(account, number, force=True)
    current = marking.order(account['id'], number)
    assert current['status'] == 'collecting' and len(marking.codes(current)) == 1
    enter(account, user, number, sku, {'gtd': GTD})
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'


def test_rescanning_removed_code_in_child_that_kept_parent_number_does_not_repeat_split(account, user, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, 'split_orders_on_pack', True)
    api, number, sku = accepted(account, user, quantity=2)
    original = marking.codes(marking.order(account['id'], number), full=True)[0]['raw']
    other = db.query_one('SELECT child_id FROM cargo_split_children WHERE account_id=? AND parent_id=? AND child_id<>?',
                         (account['id'], number, number))['child_id']
    other_code = marking.codes(marking.order(account['id'], other), full=True)[0]['raw']
    remove_remotely(api, number)
    packing.release(user)
    scan(account, user, BARCODE)
    assert scan(account, user, original)['action'] == 'marking_saved'
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'
    assert marking.codes(marking.order(account['id'], other), full=True)[0]['raw'] == other_code
    assert len(api.marking_requests) == 2


@pytest.mark.parametrize('kind', ['KIZ', 'DATA_MATRIX', 'new_type'])
def test_changed_api_mark_type_is_not_treated_as_code_deletion(account, user, monkeypatch, kind):
    api, number, sku = accepted(account, user)
    original = api.exemplars_get
    def renamed_type(*args):
        data = original(*args)
        for p in data['products']:
            for e in p['exemplars']:
                for m in e.get('marks') or []:
                    m['mark_type'] = kind
        return data
    monkeypatch.setattr(api, 'exemplars_get', renamed_type)
    assert packing.release(user)['status'] == 'ok'
    saved = marking.order(account['id'], number)
    assert saved['status'] == 'accepted' and len(marking.codes(saved)) == 1


def test_single_unit_with_recreated_exemplar_id_can_be_rescanned(account, user):
    api, number, sku = accepted(account, user)
    raw = marking.codes(marking.order(account['id'], number), full=True)[0]['raw']
    exemplar = api._marking_payloads[number]['products'][0]['exemplars'][0]
    old_id = exemplar['exemplar_id']
    exemplar['exemplar_id'] = old_id + 100
    remove_remotely(api, number)
    assert packing.release(user)['status'] == 'ok'
    assert marking.codes(marking.order(account['id'], number)) == []
    scan(account, user, BARCODE)
    assert scan(account, user, raw)['action'] == 'marking_saved'
    finish_job(account, number)
    assert marking.order(account['id'], number)['status'] == 'accepted'
    assert api.marking_requests[-1]['products'][0]['exemplars'][0]['exemplar_id'] == old_id + 100
