"""Изменённая в кабинете Маркета раскладка заменяет локальные места и PDF."""
import io
from copy import deepcopy
from unittest.mock import Mock

import pytest
from pypdf import PdfReader

from app.core import assembly_stickers, cargo, db, marking, packing, sticker_archive
from app.core.config import settings
from app.markets.yandex import client as yandex, pack, sync
from tests.core.test_cargo import ordinary_yandex
from tests.core.test_marking import BARCODE, code, finish_job, marked_yandex, scan


def pages(pdf):
    return len(PdfReader(io.BytesIO(pdf)).pages)


@pytest.fixture(autouse=True)
def options(monkeypatch):
    monkeypatch.setattr(settings, 'split_orders_on_pack', True)
    monkeypatch.setattr(settings, 'autoprint', False)


@pytest.fixture
def divided(yandex_account, user):
    api, number, key = ordinary_yandex(yandex_account, 1, ready=True)
    assert scan(yandex_account, user, BARCODE)['status'] == 'ok'
    saved = cargo.open_editor(yandex_account, number, user)
    unit = saved['units'][0]['id']
    cargo.save_editor(yandex_account, number, user, saved['revision'],
                      [{'id': 'box-1', 'units': [unit]}, {'id': 'box-2', 'units': [unit]}])
    assert pages(assembly_stickers.load(yandex_account, number, user)) == 2
    return api, number, key


def remove_layout(api, number):
    api._orders[number]['delivery']['boxesLayout'] = []
    api.boxes[number] = 1


@pytest.mark.parametrize('explicit_empty', [True, False])
def test_release_and_reopen_remove_external_split_and_old_labels(yandex_account, user, divided, monkeypatch, explicit_empty):
    api, number, _key = divided
    before = cargo.get(yandex_account['id'], number)
    original = dict(db.query_one('SELECT * FROM sticker_versions'))
    remove_layout(api, number)
    if not explicit_empty:
        api._orders[number]['delivery'].pop('boxesLayout')
    writer = Mock(side_effect=AssertionError('must not restore deleted split'))
    monkeypatch.setattr(api, 'order_boxes', writer)
    assert packing.release(user)['status'] == 'ok'
    after = cargo.get(yandex_account['id'], number)
    assert len(after['boxes']) == 1
    assert after['revision'] > before['revision'] and after['checked'] == []
    assert not db.query('SELECT * FROM cargo_barcodes WHERE account_id=?', (yandex_account['id'],))
    assert not db.query('SELECT * FROM cargo_labels WHERE account_id=?', (yandex_account['id'],))
    reopened = scan(yandex_account, user, number)
    assert reopened['status'] == 'ok', reopened
    assert len(reopened['state']['cargo']['boxes']) == 1
    assert pages(assembly_stickers.load(yandex_account, number, user)) == 1
    assert pages(pack.labels(yandex_account, user, [number], mark=False)[0]) == 1
    assert pages(sticker_archive.version(original['id'])['pdf']) == 1
    assert bytes(db.query_one('SELECT pdf FROM sticker_versions')['pdf']) == bytes(original['pdf'])
    assert len(db.query('SELECT * FROM sticker_versions')) == 1
    assert not writer.called


def test_sync_adopts_one_remote_box_and_invalidates_checked_barcodes(yandex_account, user, divided):
    api, number, key = divided
    saved = cargo.get(yandex_account['id'], number)
    db.execute('UPDATE cargo_orders SET checked=? WHERE account_id=? AND order_id=?',
               ('["old-checked"]', yandex_account['id'], number))
    api.order_boxes(str(api._orders[number]['campaignId']), number, [{'items': [{'id': int(key), 'fullCount': 1}]}])
    sync.sync_yandex(yandex_account)
    current = cargo.get(yandex_account['id'], number)
    assert len(current['boxes']) == 1 and not current['checked']
    assert current['revision'] > saved['revision']
    assert current['boxes'][0]['remote_id'] != saved['boxes'][0]['remote_id']
    assert cargo.label_order(yandex_account['id'], saved['boxes'][1]['barcode']) is None
    assert pages(assembly_stickers.load(yandex_account, number, user)) == 1


def test_print_without_reopening_reads_remote_layout(yandex_account, user, divided):
    api, number, _key = divided
    remove_layout(api, number)
    pdf, _name = pack.labels(yandex_account, user, [number], mark=False)
    assert pages(pdf) == 1
    assert len(packing.state(user)['cargo']['boxes']) == 1


def test_reopen_refreshes_even_if_automatic_splitting_is_disabled(yandex_account, user, divided, monkeypatch):
    api, number, _key = divided
    # Сборку отменили до изменения в кабинете, а затем отключили авторазделение.
    packing.release(user)
    remove_layout(api, number)
    monkeypatch.setattr(settings, 'split_orders_on_pack', False)
    result = scan(yandex_account, user, number)
    assert result['status'] == 'ok', result
    assert len(result['state']['cargo']['boxes']) == 1
    assert pages(assembly_stickers.load(yandex_account, number, user)) == 1


def test_synced_list_missing_layout_does_not_override_order_details(yandex_account, user, divided, monkeypatch):
    api, number, _key = divided
    original = api.orders

    def listing(*args, **kwargs):
        orders, token = original(*args, **kwargs)
        orders = deepcopy(orders)
        for order in orders:
            order['delivery'].pop('boxesLayout', None)
        return orders, token

    monkeypatch.setattr(api, 'orders', listing)
    sync.sync_yandex(yandex_account)
    assert len(cargo.get(yandex_account['id'], number)['boxes']) == 2
    assert pages(assembly_stickers.load(yandex_account, number, user)) == 2


def test_cancelled_split_keeps_full_mark_and_its_unit(yandex_account, user):
    api, number, key = marked_yandex(yandex_account)
    scan(yandex_account, user, BARCODE)
    scan(yandex_account, user, code())
    finish_job(yandex_account, number)
    saved = cargo.open_editor(yandex_account, number, user)
    unit = saved['units'][0]['id']
    cargo.save_editor(yandex_account, number, user, saved['revision'],
                      [{'id': 'box-1', 'units': [unit]}, {'id': 'box-2', 'units': [unit]}])
    assert pages(assembly_stickers.load(yandex_account, number, user)) == 2
    before = marking.codes(marking.order(yandex_account['id'], number), full=True)
    api.order_boxes(str(api._orders[number]['campaignId']), number,
                    [{'items': [{'id': int(key), 'fullCount': 1, 'instances': [{'cis': code()}]}]}])
    packing.release(user)
    after = marking.codes(marking.order(yandex_account['id'], number), full=True)
    assert before == after and after[0]['raw'] == code() and after[0]['unit_no'] == 1
    assert marking.order(yandex_account['id'], number)['status'] == 'accepted'
    assert pages(assembly_stickers.load(yandex_account, number, user)) == 1


def test_unavailable_api_does_not_erase_layout_and_cancel_still_works(yandex_account, user, divided, monkeypatch):
    api, number, _key = divided
    before = cargo.get(yandex_account['id'], number)
    monkeypatch.setattr(api, 'order_details', Mock(side_effect=yandex.YandexError('timeout')))
    assert packing.release(user)['status'] == 'ok'
    assert not packing.state(user)['active']
    assert cargo.get(yandex_account['id'], number) == before
    with pytest.raises((ValueError, yandex.YandexError)):
        assembly_stickers.load(yandex_account, number, user)


def test_open_editor_adopts_removed_layout_without_cancelling(yandex_account, user, divided):
    api, number, _key = divided
    before = cargo.get(yandex_account['id'], number)
    remove_layout(api, number)
    saved = cargo.open_editor(yandex_account, number, user)
    assert len(saved['boxes']) == 1 and saved['revision'] > before['revision']
    assert saved['status'] == 'baseline'


def test_return_to_started_does_not_restore_the_deleted_split(yandex_account, user, monkeypatch):
    api, number, _key = ordinary_yandex(yandex_account, 2)
    assert scan(yandex_account, user, BARCODE)['status'] == 'ok'
    assert len(cargo.get(yandex_account['id'], number)['boxes']) == 2
    remove_layout(api, number)
    api._orders[number]['substatus'] = 'STARTED'
    writer = Mock(side_effect=AssertionError('must not restore split after cabinet edit'))
    monkeypatch.setattr(api, 'order_boxes', writer)
    packing.release(user)
    result = scan(yandex_account, user, number)
    assert result['status'] == 'ok', result
    assert result['state']['total'] == 2
    assert len(result['state']['cargo']['boxes']) == 1
    assert not writer.called


def test_incomplete_remote_box_ids_block_current_print(yandex_account, user, divided):
    api, number, _key = divided
    api._orders[number]['delivery']['boxesLayout'][1].pop('barcode')
    with pytest.raises(ValueError, match='идентификаторы'):
        assembly_stickers.load(yandex_account, number, user)
    # Сохраняем исходные данные до полного подтверждения площадкой.
    assert len(cargo.get(yandex_account['id'], number)['boxes']) == 2


def test_uncertain_local_write_is_not_replaced_by_a_refresh(yandex_account, user, divided):
    api, number, _key = divided
    db.execute("UPDATE cargo_orders SET status='uncertain',pending=boxes WHERE account_id=? AND order_id=?",
               (yandex_account['id'], number))
    before = cargo.get(yandex_account['id'], number)
    remove_layout(api, number)
    assert cargo.refresh(yandex_account, number) == before
    packing.release(user)
    assert cargo.get(yandex_account['id'], number) == before


def test_unchanged_layout_keeps_v1670_cache_and_offline_archive(yandex_account, user, divided, monkeypatch):
    import hashlib
    import json
    api, number, _key = divided
    saved = cargo.get(yandex_account['id'], number)
    # Ключи записанного версией 1.67.0 кэша должны читаться и после обновления.
    shape = [(b['id'], sorted(b['units'])) for b in saved['boxes']]
    for box in saved['boxes']:
        previous_signature = hashlib.sha256(json.dumps([shape, box['id'], box['barcode']], sort_keys=True).encode()).hexdigest()
        db.execute('UPDATE cargo_labels SET signature=? WHERE account_id=? AND order_id=? AND box_id=?',
                   (previous_signature, yandex_account['id'], number, box['remote_id']))
    original = db.query_one('SELECT id FROM sticker_versions')
    monkeypatch.setattr(api, 'box_label', Mock(side_effect=AssertionError('unchanged label was downloaded again')))
    assert pages(assembly_stickers.load(yandex_account, number, user)) == 2
    assert cargo.get(yandex_account['id'], number)['revision'] == saved['revision']
    monkeypatch.setattr(api, 'order_details', Mock(side_effect=yandex.YandexError('offline')))
    assert pages(sticker_archive.version(original['id'])['pdf']) == 2
