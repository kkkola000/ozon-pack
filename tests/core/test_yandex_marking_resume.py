"""Explicit rescans recover acknowledged missing CIS; unknown dispatch stays read-only."""
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.core import db, marking, packing
from app.main import app
from app.markets.yandex.client import YandexError
from tests.core.test_api import login
from tests.core.test_marking import BARCODE, code, finish_job, marked_yandex, scan


def pending(account, user, monkeypatch, uncertain=False):
    api, number, key = marked_yandex(account)
    scan(account, user, BARCODE)
    scan(account, user, code())
    with monkeypatch.context() as patch:
        if uncertain:
            patch.setattr(api, 'order_boxes', Mock(side_effect=YandexError('synthetic timeout')))
        else:
            patch.setattr(api, 'order_boxes', Mock(return_value={'status': 'OK'}))
        marking.process(account, number, force=True)
    assert marking.order(account['id'], number)['status'] == ('uncertain' if uncertain else 'checking')
    assert api._orders[number]['items'][0].get('instances', []) == []
    return api, number, key


def test_cancel_reopen_rescan_restarts_transmission_without_a_duplicate_code_or_print(yandex_account, user, monkeypatch):
    api, number, _ = pending(yandex_account, user, monkeypatch)
    original = marking.codes(marking.order(yandex_account['id'], number))[0]
    packing.release(user)
    scan(yandex_account, user, BARCODE)
    result = scan(yandex_account, user, code())
    assert 'в панели' in result['message'] and 'возобновлена' in result['message']
    assert 'marking_print' not in result
    marking.process(yandex_account, number, force=True)  # fresh read + explicit rescan queues a retry
    assert marking.order(yandex_account['id'], number)['status'] == 'queued'
    finish_job(yandex_account, number)
    saved = marking.order(yandex_account['id'], number)
    assert saved['status'] == 'accepted' and saved['rescan_requested'] == 0
    current = marking.codes(saved)
    assert len(current) == 1 and current[0]['id'] == original['id']
    assert packing.state(user)['items'][0]['marking']['status'] == 'accepted'
    assert api._orders[number]['substatus'] == 'READY_TO_SHIP'


def test_rescan_after_an_unknown_write_does_not_replay_it(yandex_account, user, monkeypatch):
    api, number, _ = pending(yandex_account, user, monkeypatch, uncertain=True)
    packing.release(user)
    scan(yandex_account, user, BARCODE)
    scan(yandex_account, user, code())
    writer = Mock(side_effect=AssertionError('uncertain write must only be read back'))
    monkeypatch.setattr(api, 'order_boxes', writer)
    for _ in range(3):
        marking.process(yandex_account, number, force=True)
    assert marking.order(yandex_account['id'], number)['status'] == 'uncertain'
    writer.assert_not_called()


def test_explicit_rescan_does_not_inherit_the_old_backoff(yandex_account, user, monkeypatch):
    _, number, _ = pending(yandex_account, user, monkeypatch)
    db.execute('UPDATE marking_orders SET attempts=12,checked_at=? WHERE account_id=? AND order_id=?',
               (db.now_iso(), yandex_account['id'], number))
    scan(yandex_account, user, code())
    marking.process(yandex_account, number)
    assert marking.order(yandex_account['id'], number)['status'] == 'queued'
    marking.process(yandex_account, number)
    assert marking.order(yandex_account['id'], number)['status'] == 'checking'
    finish_job(yandex_account, number)
    assert marking.order(yandex_account['id'], number)['status'] == 'accepted'


@pytest.mark.parametrize('response', ['in_progress', 'malformed', 'permission'])
def test_rescan_does_not_rewrite_a_pending_or_inaccessible_remote_result(yandex_account, user, monkeypatch, response):
    api, number, key = pending(yandex_account, user, monkeypatch)
    scan(yandex_account, user, code())
    if response == 'permission':
        monkeypatch.setattr(api, 'identifiers_status', Mock(side_effect=YandexError('private error', status=403)))
    else:
        data = {'status': 'OK'} if response == 'malformed' else {'status': 'OK', 'result': {'items': [
            {'id': int(key), 'cis': [{'value': code(), 'status': 'IN_PROGRESS'}]}]}}
        monkeypatch.setattr(api, 'identifiers_status', Mock(return_value=data))
    writer = Mock(side_effect=AssertionError('no safe retry proof'))
    monkeypatch.setattr(api, 'order_boxes', writer)
    marking.process(yandex_account, number, force=True)
    assert marking.order(yandex_account['id'], number)['status'] == 'checking'
    writer.assert_not_called()


def test_pending_cis_is_visible_in_stickers_before_the_pdf_is_available(yandex_account, user, monkeypatch):
    _, number, _ = pending(yandex_account, user, monkeypatch)
    saved = marking.order(yandex_account['id'], number)
    assert not db.query('SELECT id FROM sticker_versions WHERE archive_id=?', (saved['archive_id'],))
    packing.release(user)
    with TestClient(app, follow_redirects=False) as client:
        login(client)
        page = client.get('/reports/stickers', params={'q': number})
        assert page.status_code == 200
        assert f'data-archive-id="{saved["archive_id"]}"' in page.text
        assert f'data-marking-archive="{saved["archive_id"]}"' in page.text
        assert 'Ожидает загрузки' in page.text
        details = client.get(f'/api/stickers/{saved["archive_id"]}/marking').json()
        assert len(details['items']) == 1 and details['items'][0]['status'] != 'accepted'
