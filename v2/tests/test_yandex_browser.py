"""Existing design and scanner controls with a Yandex account and an isolated DB."""

import shutil

import pytest
from conftest import login
from playwright.sync_api import expect, sync_playwright
from test_instance_data import open_window
from test_yandex import pipeline as pipeline
from test_yandex import settle
from yandex_remote import BARCODE, GTD, ORDER, mark


@pytest.mark.skipif(not shutil.which("chromium"), reason="System Chromium required")
def test_yandex_scanner_gtd_status_and_archive(pipeline, owner, client):
    assembly, data, remote, _ = pipeline
    view = open_window(pipeline, owner, ORDER)
    client.app.state.assembly = assembly
    client.app.state.connections = assembly.connections
    client.app.state.catalog = assembly.orders.catalog
    client.app.state.orders = assembly.orders
    client.app.state.instances = data
    login(client)
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=shutil.which("chromium"), args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1360, "height": 960})
        page.on("pageerror", lambda error: errors.append(str(error)))

        def request(route):
            req = route.request
            response = client.request(req.method, req.url, headers=req.headers, content=req.post_data_buffer)
            route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)

        page.route("http://testserver/**", request)
        page.route("https://**", lambda route: route.abort())
        page.goto("http://testserver/pack")
        expect(page.locator("[data-assembly-account]")).to_contain_text("Яндекс Маркет")
        expect(page.get_by_role("checkbox", name="Не указывать")).to_have_count(0)
        expect(page.locator("[data-layout-unit]")).to_have_count(0)
        scanner = page.locator("#assembly-code")
        scanner.fill(BARCODE)
        scanner.press("Enter")
        expect(page.locator("[data-assembly-count]")).to_have_text("1 из 1")
        gtd = page.locator("[data-gtd-unit]")
        gtd.fill(GTD)
        gtd.press("Enter")
        expect(page.locator("[data-assembly-message]")).to_have_text("ГТД сохранена")
        scanner.fill(mark())
        scanner.press("Enter")
        expect(page.locator("[data-scanner-state]")).to_have_text("Проверяем данные…")
        settle(pipeline, owner, view)
        remote.cis_status = "OK"
        settle(pipeline, owner, view)
        expect(page.locator(".assembly-mark.accepted")).to_have_text("М", timeout=12000)
        expect(page.get_by_text("Проверка успешно пройдена.", exact=True)).to_be_visible()
        expect(page.locator(".assembly-gtd .access-ok")).to_have_text("ГТД принята Яндекс Маркетом")
        page.get_by_role("button", name="Проверить товары", exact=True).click()
        expect(page.locator("[data-assembly-stage]")).to_have_text("Товары проверены")
        expect(page.get_by_role("button", name="Проверить товары", exact=True)).to_be_hidden()
        assert not errors
        page.screenshot(path="/tmp/ozon-pack-v2-yandex-browser.png", full_page=True)
        page.goto("http://testserver/reports/stickers")
        expect(page.get_by_role("button", name="Честный знак")).to_be_visible()
        expect(page.get_by_text(ORDER, exact=True)).to_be_visible()
        expect(page.get_by_text("Яндекс Маркет", exact=True)).to_be_visible()
        assert not errors
        browser.close()
