"""A hidden historical code must not reserve an order after a proven replacement."""
from copy import deepcopy

import pytest

from app.core import db, marking, marking_codes, marking_registry, marking_replacements, packing
from app.markets.ozon.client import OzonError
from app.markets.yandex.client import YandexError
from tests.core.test_marking import BARCODE, code, finish_job, marked_ozon, marked_yandex, scan


def fingerprint(raw):
    return marking._fingerprint(marking_codes.parse(raw))


def replaced(shop, user, monkeypatch, *, keep_legacy=False, quantity=1):
    ozon = shop['marketplace'] == 'ozon'
    api, number, _ = (marked_ozon if ozon else marked_yandex)(shop, quantity=quantity)
    original, replacement = code(), code('ABCDEF1234568')
    scan(shop, user, BARCODE)
    scan(shop, user, original)
    for unit in range(2, quantity + 1):
        scan(shop, user, BARCODE)
        scan(shop, user, code(f'ABCDEF12345{unit:02}'))
    method, error = ('exemplars_set', OzonError) if ozon else ('order_boxes', YandexError)

    def reject(*_):
        raise error('synthetic rejection', status=422)

    with monkeypatch.context() as patch:
        patch.setattr(api, method, reject)
        marking.process(shop, number, force=True)
    assert marking.order(shop['id'], number)['status'] == 'error'
    assert scan(shop, user, replacement)['action'] == 'marking_saved'
    assert marking_registry.binding(fingerprint(original))['state'] == 'bound'
    for unit in range(2, quantity + 1):
        scan(shop, user, code(f'ABCDEF12345{unit:02}'))
    with monkeypatch.context() as patch:
        if keep_legacy:
            patch.setattr(marking_replacements, 'reconcile', lambda *_: 0)
        finish_job(shop, number)
    assert marking.order(shop['id'], number)['status'] == 'accepted'
    return api, number, original, replacement


@pytest.fixture(params=['ozon', 'yandex'])
def shop(request, account, yandex_account):
    return account if request.param == 'ozon' else yandex_account


def test_replacement_releases_only_the_old_identity_and_keeps_history(shop, user, monkeypatch):
    _, number, original, replacement = replaced(shop, user, monkeypatch)
    saved = marking.order(shop['id'], number)
    assert marking_registry.binding(fingerprint(original))['state'] == 'released'
    assert marking_registry.binding(fingerprint(replacement))['state'] == 'bound'
    current = marking.codes(saved, full=True)
    assert len(current) == 1 and current[0]['raw'] == replacement and current[0]['status'] == 'accepted'
    history = marking_registry.history(saved['archive_id'])
    assert len(history) == 1 and history[0]['reason'] == 'replaced'
    assert marking._open(history[0]['code']) == original
    details = marking.archive_details(saved['archive_id'])['items']
    assert [(m['status'], m['history']) for m in details] == [('accepted', False), ('detached', True)]
    assert marking_replacements.reconcile(shop, number) == 0
    db.init_db()
    assert marking_registry.binding(fingerprint(original))['state'] == 'released'
    assert len(marking_registry.history(saved['archive_id'])) == 1
    assert packing.state(user)['items'][0]['marking']['status'] == 'accepted'


def test_worker_repairs_legacy_reservations_without_resending_codes(shop, user, monkeypatch):
    api, number, original, _ = replaced(shop, user, monkeypatch, keep_legacy=True)
    assert marking_registry.binding(fingerprint(original))['state'] == 'bound'
    method = 'exemplars_set' if shop['marketplace'] == 'ozon' else 'order_boxes'

    def no_write(*_):
        pytest.fail('cleanup must not write marketplace instances')

    monkeypatch.setattr(api, method, no_write)
    marking.run_once()
    assert marking_registry.binding(fingerprint(original))['state'] == 'released'
    assert marking.order(shop['id'], number)['status'] == 'accepted'


def test_released_code_can_be_used_in_another_market_without_losing_history(shop, account, yandex_account, user, monkeypatch):
    _, number, original, _ = replaced(shop, user, monkeypatch)
    saved = marking.order(shop['id'], number)
    packing.release(user)
    peer = yandex_account if shop['marketplace'] == 'ozon' else account
    (marked_yandex if peer['marketplace'] == 'yandex' else marked_ozon)(peer)
    scan(peer, user, BARCODE)
    assert scan(peer, user, original)['action'] == 'marking_saved'
    owner = marking_registry.binding(fingerprint(original))
    assert owner['account_id'] == peer['id'] and owner['state'] == 'bound'
    assert marking._open(marking_registry.history(saved['archive_id'])[0]['code']) == original


def alter_remote_units(ozon, units, original, failure):
    if failure == 'old_present':
        if ozon:
            units[0]['marks'].append({'mark_type': 'mandatory_mark', 'mark': original})
        else:
            units.append({'cisFull': original, 'cis': marking_codes.short(original)})
    elif failure == 'short_only':
        if ozon:
            units[0]['marks'][0]['mark'] = marking_codes.short(original)
        else:
            units[0].pop('cisFull')


def alter_remote_boxes(ozon, raw, original, failure):
    if not ozon and failure in {'missing_unit', 'short_only'}:
        from tests.markets.yandex.test_marking_status import replace_box_instances
        instances = [{'cis': marking_codes.short(original)}] if failure == 'short_only' else []
        replace_box_instances(raw, instances)


@pytest.mark.parametrize('failure', ['403', 'missing_product', 'missing_unit', 'old_present', 'short_only', 'changed_identity', 'not_checked'])
def test_incomplete_or_failed_proof_does_not_release_anything(shop, user, monkeypatch, failure):
    api, number, original, _ = replaced(shop, user, monkeypatch, keep_legacy=True)
    ozon = shop['marketplace'] == 'ozon'
    read = 'exemplars_get' if ozon else 'order_details'
    source = getattr(api, read)

    def response(*args):
        if failure == '403':
            raise (OzonError if ozon else YandexError)('secret response', status=403)
        raw = deepcopy(source(*args))
        product = raw['products' if ozon else 'items'][0]
        units = product['exemplars' if ozon else 'instances']
        if failure == 'missing_product':
            raw['products' if ozon else 'items'] = []
        elif failure == 'missing_unit':
            units.clear()
        elif failure == 'changed_identity':
            if ozon:
                units[0]['exemplar_id'] += 1
            else:
                raw['campaignId'] += 1
        else:
            alter_remote_units(ozon, units, original, failure)
        alter_remote_boxes(ozon, raw, original, failure)
        return raw

    monkeypatch.setattr(api, read, response)
    if failure == 'not_checked':
        check_method = 'exemplars_status' if ozon else 'identifiers_status'
        check_source = getattr(api, check_method)

        def pending(*args):
            raw = deepcopy(check_source(*args))
            if ozon:
                raw['products'][0]['exemplars'][0]['marks'][0]['check_status'] = 'pending'
            else:
                raw['result']['items'][0]['cis'][0]['status'] = 'IN_PROGRESS'
            return raw

        monkeypatch.setattr(api, check_method, pending)
    assert marking_replacements.reconcile(shop, number) == 0
    saved = marking.order(shop['id'], number)
    assert saved['status'] == 'accepted'
    assert marking_registry.binding(fingerprint(original))['state'] == 'bound'
    assert marking_registry.history(saved['archive_id']) == []


@pytest.mark.parametrize('phase', ['queued', 'checking', 'uncertain', 'not_recorded', 'error'])
def test_unconfirmed_order_cannot_release_a_hidden_reservation(shop, user, monkeypatch, phase):
    _, number, original, _ = replaced(shop, user, monkeypatch, keep_legacy=True)
    saved = marking.order(shop['id'], number)
    db.execute('UPDATE marking_orders SET status=? WHERE id=?', (phase, saved['id']))
    assert marking_replacements.reconcile(shop, number) == 0
    assert marking_registry.binding(fingerprint(original))['state'] == 'bound'


def test_order_revision_change_during_remote_check_prevents_release(shop, user, monkeypatch):
    _, number, original, _ = replaced(shop, user, monkeypatch, keep_legacy=True)
    provider = marking.source(shop)
    original_snapshot = provider.replacement_snapshot

    def changed(*args):
        result = original_snapshot(*args)
        db.execute('UPDATE marking_orders SET revision=revision+1 WHERE account_id=? AND order_id=?', (shop['id'], number))
        return result

    from dataclasses import replace
    updated = replace(provider, replacement_snapshot=changed)
    monkeypatch.setattr(marking, 'source', lambda *_: updated)
    assert marking_replacements.reconcile(shop, number) == 0
    assert marking_registry.binding(fingerprint(original))['state'] == 'bound'


def test_each_physical_unit_keeps_its_own_current_code(shop, user, monkeypatch):
    _, number, original, replacement = replaced(shop, user, monkeypatch, quantity=2)
    saved = marking.order(shop['id'], number)
    current = marking.codes(saved)
    assert len(current) == 2 and {m['unit_no'] for m in current} == {1, 2}
    assert all(m['status'] == 'accepted' and marking_registry.binding(m['fingerprint'])['state'] == 'bound' for m in current)
    assert marking_registry.binding(fingerprint(original))['state'] == 'released'
    assert marking_registry.binding(fingerprint(replacement))['state'] == 'bound'


def test_conflict_scan_repairs_a_legacy_hidden_binding(shop, account, yandex_account, user, monkeypatch):
    _, number, original, _ = replaced(shop, user, monkeypatch, keep_legacy=True)
    packing.release(user)
    peer = yandex_account if shop['marketplace'] == 'ozon' else account
    (marked_yandex if peer['marketplace'] == 'yandex' else marked_ozon)(peer)
    scan(peer, user, BARCODE)
    assert scan(peer, user, original)['action'] == 'marking_saved'
    assert marking_registry.binding(fingerprint(original))['account_id'] == peer['id']
    assert marking.order(shop['id'], number)['status'] == 'accepted'


def test_database_upgrade_keeps_legacy_codes_and_adds_verification_columns(shop, user, monkeypatch):
    _, number, original, replacement = replaced(shop, user, monkeypatch, keep_legacy=True)
    db.execute('DROP INDEX idx_marking_replacement_due')
    db.execute('ALTER TABLE marking_orders DROP COLUMN replacements_checked_at')
    for column in ('check_status', 'check_substatus', 'check_message'):
        db.execute(f'ALTER TABLE marking_codes DROP COLUMN {column}')
    db.init_db()
    saved = marking.order(shop['id'], number)
    assert saved['replacements_checked_at'] is None and saved['status'] == 'accepted'
    current = marking.codes(saved, full=True)
    assert len(current) == 1 and current[0]['raw'] == replacement and current[0]['check_status'] is None
    assert marking_registry.binding(fingerprint(original))['state'] == 'bound'
    assert marking_replacements.reconcile(shop, number) == 1


def test_failed_cleanup_is_throttled_and_does_not_claim_success(shop, user, monkeypatch):
    api, number, original, _ = replaced(shop, user, monkeypatch, keep_legacy=True)
    read = 'exemplars_get' if shop['marketplace'] == 'ozon' else 'order_details'
    calls = []

    def unavailable(*_):
        calls.append(True)
        raise TimeoutError('private code and API key')

    monkeypatch.setattr(api, read, unavailable)
    marking_replacements.run_once()
    marking_replacements.run_once()
    assert len(calls) == 1
    assert marking_registry.binding(fingerprint(original))['state'] == 'bound'
    assert marking.order(shop['id'], number)['status'] == 'accepted'


@pytest.mark.parametrize('phase', ['saved', 'accepted', 'uncertain'])
def test_second_scan_cannot_overwrite_a_code_with_a_pending_or_accepted_write(shop, user, monkeypatch, phase):
    ozon = shop['marketplace'] == 'ozon'
    api, number, _ = (marked_ozon if ozon else marked_yandex)(shop)
    scan(shop, user, BARCODE)
    scan(shop, user, code())
    if phase == 'accepted':
        finish_job(shop, number)
    elif phase == 'uncertain':
        method = 'exemplars_set' if ozon else 'order_boxes'
        original = getattr(api, method)

        def timed_out(*args):
            original(*args)
            raise (OzonError if ozon else YandexError)('synthetic timeout')

        with monkeypatch.context() as patch:
            patch.setattr(api, method, timed_out)
            marking.process(shop, number, force=True)
        assert marking.order(shop['id'], number)['status'] == 'uncertain'
    other = code('ABCDEF1234568')
    assert scan(shop, user, other)['status'] == 'error'
    assert marking_registry.binding(fingerprint(other)) is None
    assert [m['raw'] for m in marking.codes(marking.order(shop['id'], number), full=True)] == [code()]


def test_a_changed_registry_revision_is_not_overwritten_by_cleanup(shop, user, monkeypatch):
    _, number, original, _ = replaced(shop, user, monkeypatch, keep_legacy=True)
    provider = marking.source(shop)
    snapshot = provider.replacement_snapshot

    def changed(*args):
        result = snapshot(*args)
        db.execute('UPDATE marking_registry SET revision=revision+1 WHERE fingerprint=?', (fingerprint(original),))
        return result

    from dataclasses import replace
    monkeypatch.setattr(marking, 'source', lambda *_: replace(provider, replacement_snapshot=changed))
    assert marking_replacements.reconcile(shop, number) == 0
    assert marking_registry.binding(fingerprint(original))['state'] == 'bound'


def test_a_failed_audit_write_rolls_back_release_and_history(shop, user, monkeypatch):
    _, number, original, _ = replaced(shop, user, monkeypatch, keep_legacy=True)
    saved = marking.order(shop['id'], number)

    def failed_audit(*_, **_kw):
        raise RuntimeError('synthetic database failure')

    monkeypatch.setattr(db, 'log_event', failed_audit)
    assert marking_replacements.reconcile(shop, number) == 0
    assert marking_registry.binding(fingerprint(original))['state'] == 'bound'
    assert marking_registry.history(saved['archive_id']) == []
    assert db.query_one('SELECT status FROM marking_codes WHERE fingerprint=?', (fingerprint(original),))['status'] == 'error'
    assert marking.order(shop['id'], number)['status'] == 'accepted'
