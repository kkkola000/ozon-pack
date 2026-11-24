"""Both marketplaces print on the opening product scan, respecting readiness."""
import pytest

from app.core import marking, packing
from app.core.config import settings
from tests.core.test_marking import BARCODE, code, finish_job, marked_ozon, marked_yandex, scan


@pytest.fixture(params=['ozon', 'yandex'])
def shop(request, account, yandex_account):
    return account if request.param == 'ozon' else yandex_account


def setup(shop, ready=False):
    if shop['marketplace'] == 'ozon':
        return marked_ozon(shop, status='awaiting_deliver' if ready else 'awaiting_packaging')
    api, number, key = marked_yandex(shop)
    if ready:
        from app.markets.yandex import store
        api._orders[number]['substatus'] = 'READY_TO_SHIP'
        from app.core import db
        with db.write() as conn:
            store.upsert_yandex_order(conn, shop['id'], api._orders[number])
    return api, number, key


def test_product_scan_automatically_prints_a_ready_label_on_both_markets(shop, user):
    _, number, _ = setup(shop, ready=True)
    result = scan(shop, user, BARCODE)
    key = 'posting_number' if shop['marketplace'] == 'ozon' else 'order_id'
    assert result['print'] == {key: number}
    assert result['state']['sticker_download']['ready'] is True
    assert not result.get('deferred_label_print')


def test_product_scan_queues_print_until_marking_and_readiness_are_confirmed(shop, user):
    _, number, _ = setup(shop)
    result = scan(shop, user, BARCODE)
    assert result.get('print') is None and result['deferred_label_print'] is True
    assert result['state']['sticker_download']['ready'] is False
    scan(shop, user, code())
    finish_job(shop, number)
    assert marking.order(shop['id'], number)['status'] == 'accepted'
    assert packing.state(user)['sticker_download']['ready'] is True


def test_scanning_an_existing_label_never_starts_automatic_print(shop, user):
    _, number, _ = setup(shop, ready=True)
    result = scan(shop, user, number)
    assert result.get('print') is None
    assert not result.get('deferred_label_print')


@pytest.mark.parametrize('ready', [True, False])
def test_disabled_autoprint_never_queues_a_label(shop, user, monkeypatch, ready):
    setup(shop, ready=ready)
    monkeypatch.setattr(settings, 'autoprint', False)
    result = scan(shop, user, BARCODE)
    assert result.get('print') is None
    assert not result.get('deferred_label_print')
