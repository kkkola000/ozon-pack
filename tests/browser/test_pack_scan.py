"""Exercise the real scanner, GTD form and HTTP routes in an isolated browser.

Optional: install Playwright and Chromium to run these alongside the Node tests.
Every HTTP request is routed to TestClient or aborted; marketplaces are fakes.
"""
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app.core import marking
from app.main import app
from tests.core.test_exemplar_data import GTD, require_ozon
from tests.core.test_marking import BARCODE, finish_job, marked_yandex

playwright = pytest.importorskip('playwright.sync_api')


@pytest.fixture
def pack_browser():
    with TestClient(app, base_url='http://panel.test') as client, playwright.sync_playwright() as pw:
        client.post('/login', data={'login': 'admin', 'password': 'test-admin-pass'})
        executable = shutil.which('chromium') or shutil.which('chromium-browser')
        if not executable and not Path(pw.chromium.executable_path).exists():
            pytest.skip('Install Chromium with playwright install chromium')
        browser = pw.chromium.launch(executable_path=executable, headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1440, 'height': 1100})
        errors, scans = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('dialog', lambda dialog: dialog.accept())

        def route_request(route):
            request = route.request
            url = urlsplit(request.url)
            if url.hostname != 'panel.test':
                route.abort()
                return
            path = url.path + ('?' + url.query if url.query else '')
            if url.path == '/api/pack/scan':
                scans.append(request.post_data_json)
            response = client.request(request.method, path, content=request.post_data_buffer,
                                      headers={k: v for k, v in request.headers.items() if k not in {'host', 'cookie'}})
            headers = {k: v for k, v in response.headers.items()
                       if k not in {'content-length', 'content-encoding', 'transfer-encoding'}}
            route.fulfill(status=response.status_code, headers=headers, body=response.content)

        page.route('**/*', route_request)
        page.goto('http://panel.test/pack')
        # Keep real scanner/form logic, but replace hardware and file downloads.
        page.evaluate('''reservePrintWindow = () => null;
                         printLabelDocument = async () => true;
                         StickerDownloads.save = async () => {};''')
        yield page, scans
        browser.close()
        assert not errors, errors


@pytest.fixture(params=['ozon', 'yandex'])
def gtd_order(request, account, yandex_account):
    if request.param == 'ozon':
        _, number, key = require_ozon(account, cz=False)
        return account, number, key
    api, number, key = marked_yandex(yandex_account)
    api._orders[number]['items'][0]['requiredInstanceTypes'] = ['GTD']
    return yandex_account, number, key


def scan(page, value):
    page.locator('#scan').fill(value)
    with page.expect_response(lambda response: urlsplit(response.url).path == '/api/pack/scan') as reply:
        page.locator('#scan').press('Enter')
    page.wait_for_function('!busy')
    return reply.value.json()


def save_gtd(page, order):
    page.get_by_role('button', name='Передать данные').click()
    playwright.expect(page.get_by_text('Проверяется площадкой…', exact=True)).to_be_visible()
    finish_job(order[0], order[1])
    assert marking.order(order[0]['id'], order[1])['status'] == 'accepted'
    page.evaluate('refreshState()')
    playwright.expect(page.locator('#scan')).to_have_attribute('placeholder', re.compile('.*отсканируйте.*'))


@pytest.mark.parametrize('choice', ['number', 'omit'])
@pytest.mark.parametrize('revert', [False, True])
def test_sticker_scan_completes_after_gtd_and_reverted_edits(pack_browser, gtd_order, choice, revert):
    page, _ = pack_browser
    assert scan(page, BARCODE)['status'] == 'ok'
    if choice == 'omit':
        page.get_by_label('Не указывать ГТД').check()
    else:
        page.get_by_placeholder('Указать ГТД').fill(GTD)
    save_gtd(page, gtd_order)
    if revert:
        if choice == 'omit':
            page.get_by_label('Не указывать ГТД').uncheck()
            page.get_by_label('Не указывать ГТД').check()
        else:
            page.get_by_placeholder('Указать ГТД').fill(GTD + '8')
            page.get_by_placeholder('Указать ГТД').fill(GTD)
    assert scan(page, gtd_order[1])['action'] == 'completed'
    playwright.expect(page.locator('#pack-done')).to_be_visible()


def test_unsent_omission_explains_block_and_completes_after_submission(pack_browser, gtd_order):
    page, requests = pack_browser
    scan(page, BARCODE)
    page.get_by_label('Не указывать ГТД').check()
    page.locator('#scan').fill(gtd_order[1])
    page.locator('#scan').press('Enter')
    playwright.expect(page.locator('#banner .text')).to_contain_text('Нажмите «Передать данные»')
    assert len(requests) == 1  # No attempt to bypass the required marketplace write.
    assert marking.order(gtd_order[0]['id'], gtd_order[1])['status'] == 'collecting'
    save_gtd(page, gtd_order)
    assert scan(page, gtd_order[1])['action'] == 'completed'


def test_cancelling_unsent_omission_does_not_block_reopening(pack_browser, gtd_order):
    page, _ = pack_browser
    scan(page, BARCODE)
    page.get_by_label('Не указывать ГТД').check()
    page.get_by_role('button', name='Отменить сборку').click()
    playwright.expect(page.locator('#pack-modal')).to_be_hidden()
    assert scan(page, BARCODE)['status'] == 'ok'
    playwright.expect(page.locator('#pack-modal')).to_be_visible()
    playwright.expect(page.get_by_label('Не указывать ГТД')).not_to_be_checked()
